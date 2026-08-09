"""Minimal fail-closed health projections for Railway and external monitoring."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Protocol

from fastapi import Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.db.models.platform import PlatformCandidateModel, RunInstanceModel
from maais.db.repositories.observability import ObservabilityRepository
from maais.observability.audit import HealthEvaluation
from maais.platform.identity import CandidateDescriptor
from maais.platform.registry import CandidateStatus, RunStatus

MONITOR_COMPONENTS = frozenset(
    {
        "database",
        "worker",
        "ledger",
        "cursors",
        "operations",
        "evidence_replication",
        "daily_close",
    }
)
_MONITOR_HEADER = b"x-maais-monitor-token"
_SCHEMA_REVISION = re.compile(r"^[0-9]{4}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class CloudEndpointSnapshot:
    """The complete public monitor projection, with no identifying metadata."""

    ready: bool
    components: Mapping[str, bool]

    def __post_init__(self) -> None:
        if type(self.ready) is not bool:
            raise TypeError("cloud endpoint readiness must be a boolean")
        if set(self.components) != MONITOR_COMPONENTS:
            raise ValueError("cloud endpoint component contract is incomplete or contains extras")
        normalized: dict[str, bool] = {}
        for name, value in self.components.items():
            if type(value) is not bool:
                raise TypeError(f"cloud endpoint component {name} must be a boolean")
            normalized[name] = value
        object.__setattr__(self, "components", MappingProxyType(normalized))


class CloudEndpointReader(Protocol):
    async def readiness(self) -> bool: ...

    async def monitor(self) -> CloudEndpointSnapshot: ...


class UnavailableCloudEndpointReader:
    """Default authority before a cloud process proves its boot identity."""

    async def readiness(self) -> bool:
        return False

    async def monitor(self) -> CloudEndpointSnapshot:
        return _unavailable_snapshot()


class DatabaseCloudEndpointReader:
    """Project candidate readiness and immutable operations health from PostgreSQL."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        expected_schema_revision: str,
        expected_candidate_hash: str,
        railway_environment_id: str,
        boot_verified: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        maximum_health_age: timedelta = timedelta(minutes=3),
    ) -> None:
        if _SCHEMA_REVISION.fullmatch(expected_schema_revision) is None:
            raise ValueError("expected schema revision must be four decimal digits")
        if _SHA256.fullmatch(expected_candidate_hash) is None:
            raise ValueError("expected candidate hash must be lowercase SHA-256")
        if not railway_environment_id or railway_environment_id != railway_environment_id.strip():
            raise ValueError("Railway environment ID must be nonempty and trimmed")
        if type(boot_verified) is not bool:
            raise TypeError("cloud endpoint boot verification must be a boolean")
        if maximum_health_age <= timedelta(0):
            raise ValueError("maximum health age must be positive")
        self._session_factory = session_factory
        self._expected_schema_revision = expected_schema_revision
        self._expected_candidate_hash = expected_candidate_hash
        self._railway_environment_id = railway_environment_id
        self._boot_verified = boot_verified
        self._clock = clock
        self._maximum_health_age = maximum_health_age

    async def readiness(self) -> bool:
        if not self._boot_verified:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                return await self._candidate_ready(session)

    async def monitor(self) -> CloudEndpointSnapshot:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                ready = self._boot_verified and await self._candidate_ready(session)
                if not ready:
                    return _unavailable_snapshot()
                run = await session.scalar(
                    select(RunInstanceModel).where(
                        RunInstanceModel.railway_environment_id == self._railway_environment_id,
                        RunInstanceModel.status == RunStatus.ACTIVE.value,
                        RunInstanceModel.candidate_hash == self._expected_candidate_hash,
                    )
                )
                if run is None:
                    return _database_only_snapshot(ready=True)
                evaluation = await ObservabilityRepository(session).latest_health(run.id)
                if evaluation is None or not self._fresh(evaluation):
                    return _database_only_snapshot(ready=True)
                return CloudEndpointSnapshot(
                    ready=True,
                    components={
                        "database": _checks_passed(
                            evaluation,
                            "database",
                            "schema_identity",
                            "cluster_identity",
                        ),
                        "worker": _checks_passed(
                            evaluation,
                            "worker_continuity",
                            "worker_lease",
                        ),
                        "ledger": _checks_passed(evaluation, "ledger"),
                        "cursors": _checks_passed(evaluation, "required_cursors"),
                        "operations": _checks_passed(
                            evaluation,
                            "dispatch_queue_capacity",
                            "deployment_identity",
                            "audit_chain",
                        ),
                        "evidence_replication": _checks_passed(
                            evaluation,
                            "backup",
                            "worm_replication",
                        ),
                        "daily_close": _checks_passed(evaluation, "daily_close"),
                    },
                )

    async def _candidate_ready(self, session: AsyncSession) -> bool:
        schema_revision = str(
            await session.scalar(text("SELECT version_num FROM public.alembic_version"))
        )
        if schema_revision != self._expected_schema_revision:
            return False
        candidate = await session.get(PlatformCandidateModel, self._expected_candidate_hash)
        if (
            candidate is None
            or candidate.status != CandidateStatus.QUALIFIED.value
            or candidate.schema_revision != self._expected_schema_revision
            or candidate.descriptor_hash != self._expected_candidate_hash
        ):
            return False
        try:
            descriptor = CandidateDescriptor.from_json_data(candidate.descriptor_json)
        except (TypeError, ValueError):
            return False
        return (
            descriptor.descriptor_hash == self._expected_candidate_hash
            and descriptor.schema_revision == self._expected_schema_revision
            and descriptor.git_sha == candidate.git_sha
        )

    def _fresh(self, evaluation: HealthEvaluation) -> bool:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("cloud endpoint clock must be UTC-aware")
        age = now.astimezone(UTC) - evaluation.checked_at.astimezone(UTC)
        return timedelta(0) <= age <= self._maximum_health_age


class InMemoryMonitorRateLimiter:
    """Bounded per-client fixed-window limiter independent from operator sessions."""

    def __init__(
        self,
        *,
        maximum_requests: int = 60,
        window_seconds: float = 60.0,
        maximum_clients: int = 1_024,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_requests <= 0:
            raise ValueError("monitor rate-limit maximum requests must be positive")
        if window_seconds <= 0:
            raise ValueError("monitor rate-limit window must be positive")
        if maximum_clients <= 0:
            raise ValueError("monitor rate-limit maximum clients must be positive")
        self._maximum_requests = maximum_requests
        self._window_seconds = window_seconds
        self._maximum_clients = maximum_clients
        self._monotonic = monotonic
        self._windows: dict[str, tuple[float, int, float]] = {}
        self._lock = threading.Lock()

    def allow(self, client_key: str) -> bool:
        if not client_key or len(client_key) > 255:
            client_key = "unknown"  # pragma: allowlist secret
        now = self._monotonic()
        with self._lock:
            current = self._windows.get(client_key)
            if current is None or now < current[0] or now - current[0] >= self._window_seconds:
                if current is None and len(self._windows) >= self._maximum_clients:
                    oldest = min(self._windows, key=lambda key: self._windows[key][2])
                    del self._windows[oldest]
                self._windows[client_key] = (now, 1, now)
                return True
            started_at, count, _last_seen = current
            if count >= self._maximum_requests:
                self._windows[client_key] = (started_at, count, now)
                return False
            self._windows[client_key] = (started_at, count + 1, now)
            return True


def monitor_token_matches(request: Request, expected_token: str) -> bool:
    """Authenticate exactly one independent monitor header using fixed-size digests."""

    supplied = [
        value for name, value in request.scope.get("headers", ()) if name.lower() == _MONITOR_HEADER
    ]
    if not expected_token or len(supplied) != 1 or not supplied[0]:
        return False
    expected_digest = hashlib.sha256(expected_token.encode("ascii")).digest()
    supplied_digest = hashlib.sha256(supplied[0]).digest()
    return hmac.compare_digest(supplied_digest, expected_digest)


def monitor_client_key(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _checks_passed(evaluation: HealthEvaluation, *names: str) -> bool:
    for name in names:
        component = evaluation.components.get(name)
        if not isinstance(component, Mapping) or component.get("status") != "ok":
            return False
    return True


def _unavailable_snapshot() -> CloudEndpointSnapshot:
    return CloudEndpointSnapshot(
        ready=False,
        components={name: False for name in MONITOR_COMPONENTS},
    )


def _database_only_snapshot(*, ready: bool) -> CloudEndpointSnapshot:
    return CloudEndpointSnapshot(
        ready=ready,
        components={name: name == "database" for name in MONITOR_COMPONENTS},
    )
