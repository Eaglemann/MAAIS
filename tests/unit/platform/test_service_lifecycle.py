from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.config.cloud import ServiceRole
from maais.platform.identity import RailwayRuntimeIdentity
from maais.platform.lifecycle import (
    ServiceLifecycleBackend,
    ServiceRoleMismatch,
    cloud_service_lifecycle,
    require_service_role,
)
from maais.platform.runtime import RuntimeIdentityEvidence
from tests.unit.config.test_cloud_settings import _railway_settings

NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
BOOT_ID = UUID("22222222-2222-4222-8222-222222222222")


class RecordingBackend(ServiceLifecycleBackend):
    def __init__(self, *, heartbeat_failure: BaseException | None = None) -> None:
        self.heartbeat_failure = heartbeat_failure
        self.registered: list[tuple[ServiceRole, UUID | None, UUID, datetime]] = []
        self.heartbeats: list[tuple[int, datetime]] = []
        self.stops: list[tuple[str, datetime]] = []
        self.closed = False
        self.two_heartbeats = asyncio.Event()

    async def verify_and_register(
        self,
        *,
        role: ServiceRole,
        run_id: UUID | None,
        boot_id: UUID,
        started_at: datetime,
    ) -> RuntimeIdentityEvidence:
        self.registered.append((role, run_id, boot_id, started_at))
        return _evidence(role=role, boot_id=boot_id, started_at=started_at)

    async def heartbeat(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        sequence: int,
        heartbeat_at: datetime,
    ) -> None:
        assert evidence.identity.boot_id == BOOT_ID
        self.heartbeats.append((sequence, heartbeat_at))
        if self.heartbeat_failure is not None:
            raise self.heartbeat_failure
        if len(self.heartbeats) == 2:
            self.two_heartbeats.set()

    async def stop(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        reason_code: str,
        stopped_at: datetime,
    ) -> None:
        assert evidence.identity.boot_id == BOOT_ID
        self.stops.append((reason_code, stopped_at))

    async def close(self) -> None:
        self.closed = True


class FailingStopBackend(RecordingBackend):
    async def stop(
        self,
        *,
        evidence: RuntimeIdentityEvidence,
        reason_code: str,
        stopped_at: datetime,
    ) -> None:
        await super().stop(
            evidence=evidence,
            reason_code=reason_code,
            stopped_at=stopped_at,
        )
        raise RuntimeError("stop evidence unavailable")


class FailingRegistrationBackend(RecordingBackend):
    async def verify_and_register(
        self,
        *,
        role: ServiceRole,
        run_id: UUID | None,
        boot_id: UUID,
        started_at: datetime,
    ) -> RuntimeIdentityEvidence:
        del role, run_id, boot_id, started_at
        raise RuntimeError("identity registration failed")


class AdvancingClock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=30)
        return value


async def immediate_interval(stop_requested: asyncio.Event, _delay: float) -> bool:
    await asyncio.sleep(0)
    return stop_requested.is_set()


@pytest.mark.parametrize("required_role", tuple(ServiceRole))
def test_role_boundary_rejects_every_other_configured_role(required_role: ServiceRole) -> None:
    configured = next(role for role in ServiceRole if role is not required_role)

    with pytest.raises(ServiceRoleMismatch, match=required_role.value):
        require_service_role(
            _railway_settings(
                service_role=configured,
                database_role_name={
                    ServiceRole.WEB: "maais_web",
                    ServiceRole.WORKER: "maais_worker",
                    ServiceRole.OPERATIONS: "maais_ops",
                    ServiceRole.VERIFIER: "maais_verifier",
                    ServiceRole.MIGRATOR: "maais_migrator",
                }[configured],
            ),
            required_role,
        )


@pytest.mark.asyncio
async def test_lifecycle_registers_once_heartbeats_monotonically_and_stops_cleanly() -> None:
    backend = RecordingBackend()
    clock = AdvancingClock()

    async with cloud_service_lifecycle(
        role=ServiceRole.WORKER,
        run_id=RUN_ID,
        settings=_railway_settings(),
        clock=clock,
        backend=backend,
        uuid_factory=lambda: BOOT_ID,
        heartbeat_interval_seconds=30,
        wait=immediate_interval,
    ) as lifecycle:
        assert lifecycle.ready is False
        lifecycle.mark_ready()
        assert lifecycle.ready is True
        await asyncio.wait_for(backend.two_heartbeats.wait(), timeout=1)

    assert backend.registered == [(ServiceRole.WORKER, RUN_ID, BOOT_ID, NOW)]
    assert [sequence for sequence, _ in backend.heartbeats] == [1, 2]
    assert backend.heartbeats[0][1] < backend.heartbeats[1][1]
    assert backend.stops == [("service_stopped", NOW + timedelta(seconds=90))]
    assert backend.closed is True


@pytest.mark.asyncio
async def test_heartbeat_failure_requests_shutdown_and_remains_terminal() -> None:
    failure = RuntimeError("heartbeat write failed")
    backend = RecordingBackend(heartbeat_failure=failure)

    with pytest.raises(RuntimeError, match="heartbeat write failed") as captured:
        async with cloud_service_lifecycle(
            role=ServiceRole.WORKER,
            run_id=RUN_ID,
            settings=_railway_settings(),
            clock=AdvancingClock(),
            backend=backend,
            uuid_factory=lambda: BOOT_ID,
            heartbeat_interval_seconds=30,
            wait=immediate_interval,
        ) as lifecycle:
            lifecycle.mark_ready()
            await lifecycle.wait_until_stopped()

    assert captured.value is failure
    assert backend.stops[0][0] == "service_failed"
    assert backend.closed is True


@pytest.mark.asyncio
async def test_stop_failure_never_masks_the_original_service_failure() -> None:
    backend = FailingStopBackend()
    original = LookupError("service body failed")

    with pytest.raises(LookupError, match="service body failed") as captured:
        async with cloud_service_lifecycle(
            role=ServiceRole.WORKER,
            run_id=RUN_ID,
            settings=_railway_settings(),
            clock=AdvancingClock(),
            backend=backend,
            uuid_factory=lambda: BOOT_ID,
            heartbeat_interval_seconds=30,
            wait=immediate_interval,
        ):
            raise original

    assert captured.value is original
    assert backend.stops[0][0] == "service_failed"
    assert backend.closed is True


@pytest.mark.asyncio
async def test_clean_body_does_not_hide_failed_stop_registration() -> None:
    backend = FailingStopBackend()

    with pytest.raises(RuntimeError, match="stop evidence unavailable"):
        async with cloud_service_lifecycle(
            role=ServiceRole.VERIFIER,
            run_id=RUN_ID,
            settings=_railway_settings(
                service_role=ServiceRole.VERIFIER,
                database_role_name="maais_verifier",
            ),
            clock=AdvancingClock(),
            backend=backend,
            uuid_factory=lambda: BOOT_ID,
            heartbeat_interval_seconds=30,
            wait=immediate_interval,
        ):
            pass

    assert backend.stops[0][0] == "service_stopped"
    assert backend.closed is True


@pytest.mark.asyncio
async def test_failed_identity_registration_still_closes_database_resources() -> None:
    backend = FailingRegistrationBackend()

    with pytest.raises(RuntimeError, match="identity registration failed"):
        async with cloud_service_lifecycle(
            role=ServiceRole.WORKER,
            run_id=RUN_ID,
            settings=_railway_settings(),
            backend=backend,
            uuid_factory=lambda: BOOT_ID,
        ):
            raise AssertionError("unreachable")

    assert backend.closed is True


def _evidence(
    *,
    role: ServiceRole,
    boot_id: UUID,
    started_at: datetime,
) -> RuntimeIdentityEvidence:
    return RuntimeIdentityEvidence(
        identity=RailwayRuntimeIdentity(
            project_id="project-1",
            environment_id="environment-1",
            service_id=f"{role.value}-service",
            deployment_id="deployment-1",
            snapshot_id="snapshot-1",
            replica_id="replica-1",
            region="europe-west4-drams3a",
            service_role=role,
            boot_id=boot_id,
            candidate_hash="a" * 64,
            started_at=started_at,
        ),
        schema_revision="0022",
        database_system_identifier_sha256="b" * 64,
    )
