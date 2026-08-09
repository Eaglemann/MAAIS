"""Shared fail-closed lifecycle for every Railway service role."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.config.cloud import DeploymentTarget, ServiceRole
from maais.config.settings import Settings
from maais.core.logging import get_logger
from maais.observability.context import bind_telemetry_context
from maais.observability.events import TelemetryContext
from maais.platform.runtime import (
    RuntimeIdentityEvidence,
    heartbeat_registered_runtime,
    load_embedded_candidate_descriptor,
    stop_registered_runtime,
    verify_and_register_runtime_evidence,
)

UTC = timezone.utc
WaitForStop = Callable[[asyncio.Event, float], Awaitable[bool]]
Clock = Callable[[], datetime]

logger = get_logger(__name__)


class ServiceRoleMismatch(RuntimeError):
    """A role-specific process was invoked with another role's authority."""


class ServiceLifecycleBackend(Protocol):
    async def verify_and_register(
        self,
        *,
        role: ServiceRole,
        run_id: UUID | None,
        boot_id: UUID,
        started_at: datetime,
    ) -> RuntimeIdentityEvidence: ...

    async def heartbeat(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        sequence: int,
        heartbeat_at: datetime,
    ) -> None: ...

    async def stop(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        reason_code: str,
        stopped_at: datetime,
    ) -> None: ...

    async def close(self) -> None: ...


class DatabaseServiceLifecycleBackend:
    """Purpose-bound database adapter retained for one service process lifetime."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine = create_async_engine(
            settings.database_url_value,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        self.session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def engine(self):
        return self._engine

    async def verify_and_register(
        self,
        *,
        role: ServiceRole,
        run_id: UUID | None,
        boot_id: UUID,
        started_at: datetime,
    ) -> RuntimeIdentityEvidence:
        require_service_role(self._settings, role)
        descriptor = load_embedded_candidate_descriptor(self._settings)
        return await verify_and_register_runtime_evidence(
            settings=self._settings,
            session_factory=self.session_factory,
            descriptor=descriptor,
            boot_id=boot_id,
            started_at=started_at,
            run_id=run_id,
        )

    async def heartbeat(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        sequence: int,
        heartbeat_at: datetime,
    ) -> None:
        await heartbeat_registered_runtime(
            session_factory=self.session_factory,
            identity=evidence.identity,
            sequence=sequence,
            heartbeat_at=heartbeat_at,
        )

    async def stop(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        reason_code: str,
        stopped_at: datetime,
    ) -> None:
        await stop_registered_runtime(
            session_factory=self.session_factory,
            identity=evidence.identity,
            reason_code=reason_code,
            stopped_at=stopped_at,
        )

    async def close(self) -> None:
        await self._engine.dispose()


class ServiceLifecycle:
    def __init__(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        stop_requested: asyncio.Event,
    ) -> None:
        self.evidence = evidence
        self.stop_requested = stop_requested
        self._ready = False
        self._terminal_error: BaseException | None = None

    @property
    def ready(self) -> bool:
        return self._ready and not self.stop_requested.is_set()

    def mark_ready(self) -> None:
        if self.stop_requested.is_set() or self._terminal_error is not None:
            raise RuntimeError("stopped service cannot become ready")
        self._ready = True

    def request_stop(self) -> None:
        self._ready = False
        self.stop_requested.set()

    async def wait_until_stopped(self) -> None:
        await self.stop_requested.wait()
        if self._terminal_error is not None:
            raise self._terminal_error

    def install_signal_handlers(self) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for handled_signal in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(handled_signal, self.request_stop)
            except (NotImplementedError, RuntimeError):
                continue
            installed.append(handled_signal)

        def remove() -> None:
            for handled_signal in installed:
                loop.remove_signal_handler(handled_signal)

        return remove

    def _fail(self, error: BaseException) -> None:
        self._terminal_error = error
        self.request_stop()


def require_service_role(settings: Settings, required_role: ServiceRole) -> None:
    if settings.deployment_target is not DeploymentTarget.RAILWAY:
        raise ServiceRoleMismatch(f"{required_role.value} service requires a Railway deployment")
    if settings.service_role is not required_role:
        configured = settings.service_role.value if settings.service_role is not None else "unset"
        raise ServiceRoleMismatch(
            f"{required_role.value} service refuses configured role {configured}"
        )


@asynccontextmanager
async def cloud_service_lifecycle(
    *,
    role: ServiceRole,
    run_id: UUID | None,
    settings: Settings,
    clock: Clock = lambda: datetime.now(UTC),
    backend: ServiceLifecycleBackend | None = None,
    uuid_factory: Callable[[], UUID] = uuid4,
    heartbeat_interval_seconds: float = 30.0,
    wait: WaitForStop | None = None,
) -> AsyncIterator[ServiceLifecycle]:
    """Verify, register, heartbeat, and durably stop exactly one service boot."""

    require_service_role(settings, role)
    if run_id is not None and run_id.int == 0:
        raise ValueError("cloud lifecycle run identifier cannot be nil")
    if heartbeat_interval_seconds <= 0:
        raise ValueError("service heartbeat interval must be positive")
    resolved_backend = backend or DatabaseServiceLifecycleBackend(settings)
    started_at = _utc(clock(), "service start")
    boot_id = uuid_factory()
    if not isinstance(boot_id, UUID) or boot_id.int == 0:
        raise ValueError("service boot identifier must be a non-nil UUID")
    try:
        evidence = await resolved_backend.verify_and_register(
            role=role,
            run_id=run_id,
            boot_id=boot_id,
            started_at=started_at,
        )
    except BaseException:
        try:
            await resolved_backend.close()
        except BaseException:
            logger.exception(
                "service_registration_cleanup_failed",
                error_code="service_registration_cleanup_failed",
                outcome="unconfirmed",
            )
        raise
    if evidence.identity.service_role is not role or evidence.identity.boot_id != boot_id:
        await resolved_backend.close()
        raise RuntimeError("registered service identity differs from lifecycle authority")

    lifecycle = ServiceLifecycle(evidence=evidence, stop_requested=asyncio.Event())
    interval_wait = wait or _wait_for_stop
    telemetry = TelemetryContext(
        service_role=role.value,
        environment=settings.environment,
        release=settings.railway_git_commit_sha,
        candidate_hash=evidence.identity.candidate_hash,
        deployment_id=evidence.identity.deployment_id,
        replica_id=evidence.identity.replica_id,
        region=evidence.identity.region,
        boot_id=evidence.identity.boot_id,
    )
    heartbeat_task: asyncio.Task[None] | None = None
    body_error: BaseException | None = None
    heartbeat_error: BaseException | None = None
    stop_error: BaseException | None = None
    close_error: BaseException | None = None
    with bind_telemetry_context(telemetry):
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                lifecycle=lifecycle,
                backend=resolved_backend,
                clock=clock,
                interval_seconds=heartbeat_interval_seconds,
                wait=interval_wait,
            ),
            name=f"cloud_{role.value}_service_heartbeat",
        )
        try:
            yield lifecycle
        except BaseException as error:
            body_error = error
            raise
        finally:
            lifecycle.request_stop()
            if heartbeat_task is not None:
                heartbeat_result = await asyncio.gather(
                    heartbeat_task,
                    return_exceptions=True,
                )
                if heartbeat_result and isinstance(heartbeat_result[0], BaseException):
                    heartbeat_error = heartbeat_result[0]
            reason_code = (
                "service_failed"
                if body_error is not None or heartbeat_error is not None
                else "service_stopped"
            )
            try:
                await resolved_backend.stop(
                    evidence=evidence,
                    reason_code=reason_code,
                    stopped_at=_utc(clock(), "service stop"),
                )
            except BaseException as error:
                stop_error = error
                logger.exception(
                    "service_stop_registration_failed",
                    error_code="service_stop_registration_failed",
                    outcome="unconfirmed",
                )
            try:
                await resolved_backend.close()
            except BaseException as error:
                close_error = error
                logger.exception(
                    "service_lifecycle_close_failed",
                    error_code="service_lifecycle_close_failed",
                    outcome="unconfirmed",
                )
            if body_error is None:
                if heartbeat_error is not None:
                    raise heartbeat_error
                if stop_error is not None:
                    raise stop_error
                if close_error is not None:
                    raise close_error


async def _heartbeat_loop(
    *,
    lifecycle: ServiceLifecycle,
    backend: ServiceLifecycleBackend,
    clock: Clock,
    interval_seconds: float,
    wait: WaitForStop,
) -> None:
    sequence = 0
    try:
        while not lifecycle.stop_requested.is_set():
            if await wait(lifecycle.stop_requested, interval_seconds):
                return
            sequence += 1
            await backend.heartbeat(
                evidence=lifecycle.evidence,
                sequence=sequence,
                heartbeat_at=_utc(clock(), "service heartbeat"),
            )
    except BaseException as error:
        lifecycle._fail(error)
        raise


async def _wait_for_stop(stop_requested: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_requested.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} time must be UTC-aware")
    return value.astimezone(UTC)
