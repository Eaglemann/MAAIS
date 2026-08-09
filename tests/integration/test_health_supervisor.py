from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from types import TracebackType
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from maais.config.cloud import ServiceRole
from maais.db.models.observability import HealthEvaluationModel
from maais.db.models.operations import IncidentModel, TradingControlModel, WorkerCheckpointModel
from maais.db.unit_of_work import UnitOfWork
from maais.market_data.recovery import MarketCursor
from maais.observability.audit import HealthSeverity, HealthStatus
from maais.operations.cloud_health import (
    CRITICAL_COMPONENTS,
    WARNING_COMPONENTS,
    CloudHealthComponent,
    CloudHealthEvaluator,
    DatabaseCloudHealthSnapshotReader,
    evaluate_cloud_components,
    reconcile_sentry_delivery_incident,
)
from maais.operations.health_supervisor import (
    HealthSupervisor,
    HealthSupervisorAlreadyRunning,
    PostgresHealthOwnership,
)
from maais.operations.incidents import IncidentStatus
from maais.orchestration.checkpoints import WorkerCheckpoint, WorkerStatus
from maais.platform.runtime import RuntimeIdentityEvidence
from tests.integration.test_platform_repository import (
    COMMAND_ONE,
    EXPERIMENT_ONE,
    NOW,
    RUN_ONE,
    WORKER_ONE,
    _prepare_activatable_run,
    _service,
)

pytestmark = pytest.mark.integration

OPERATIONS_BOOT = UUID("99999999-9999-4999-8999-999999999999")
SENTRY_INCIDENT = UUID("88888888-8888-4888-8888-888888888888")


class SnapshotReader:
    def __init__(self) -> None:
        self.failed: tuple[str, ...] = ()
        self.calls: list[tuple[UUID, datetime]] = []

    async def collect(
        self,
        run_id: UUID,
        checked_at: datetime,
    ) -> dict[str, CloudHealthComponent]:
        self.calls.append((run_id, checked_at))
        return {
            name: CloudHealthComponent(
                passed=name not in self.failed,
                failure_severity=(
                    HealthSeverity.CRITICAL
                    if name in CRITICAL_COMPONENTS
                    else HealthSeverity.WARNING
                ),
                reason_code=(f"{name}_failed" if name in self.failed else "check_passed"),
                evidence={"observed": name not in self.failed},
            )
            for name in (*sorted(CRITICAL_COMPONENTS), *sorted(WARNING_COMPONENTS))
        }


async def test_health_evaluations_deduplicate_incident_episode_and_recover_without_trading_mutation(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )
    async with uow_factory.begin() as uow:
        await uow.platform.activate_run(
            RUN_ONE,
            command_id=COMMAND_ONE,
            worker_boot_id=WORKER_ONE,
            started_at=NOW + timedelta(seconds=3),
        )
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ONE,
                boot_id=OPERATIONS_BOOT,
                role=ServiceRole.OPERATIONS,
                service_id="operations-service",
            )
        )
        controls_before = await uow.session.scalar(
            select(func.count()).select_from(TradingControlModel)
        )
        checkpoints_before = await uow.session.scalar(
            select(func.count()).select_from(WorkerCheckpointModel)
        )

    reader = SnapshotReader()
    reader.failed = ("worker_lease",)
    evaluator = CloudHealthEvaluator(
        uow_factory=uow_factory,
        snapshot_reader=reader,
        service_boot_id=OPERATIONS_BOOT,
    )

    first = await evaluator.evaluate(RUN_ONE, NOW + timedelta(minutes=1))
    second = await evaluator.evaluate(RUN_ONE, NOW + timedelta(minutes=2))
    reader.failed = ()
    recovery = await evaluator.evaluate(RUN_ONE, NOW + timedelta(minutes=3))

    assert first.overall_status is HealthStatus.CRITICAL
    assert first.incident_id is not None
    assert second.incident_id == first.incident_id
    assert recovery.overall_status is HealthStatus.HEALTHY
    assert recovery.incident_id is None
    assert recovery.recovery_of_evaluation_id == second.evaluation_id
    assert recovery.recovered_at == recovery.checked_at
    assert reader.calls == [
        (RUN_ONE, NOW + timedelta(minutes=1)),
        (RUN_ONE, NOW + timedelta(minutes=2)),
        (RUN_ONE, NOW + timedelta(minutes=3)),
    ]

    async with uow_factory.begin() as uow:
        evaluations = tuple(
            await uow.session.scalars(
                select(HealthEvaluationModel).order_by(HealthEvaluationModel.checked_at)
            )
        )
        incident = await uow.session.get(IncidentModel, first.incident_id)
        controls_after = await uow.session.scalar(
            select(func.count()).select_from(TradingControlModel)
        )
        checkpoints_after = await uow.session.scalar(
            select(func.count()).select_from(WorkerCheckpointModel)
        )
        health_audit = tuple(
            event
            for event in await uow.observability.list_audit_events()
            if event.event_code == "health.evaluated"
        )

    assert len(evaluations) == 3
    assert incident is not None
    assert incident.status == IncidentStatus.RESOLVED.value
    assert incident.resolved_by == "cloud_health"
    assert incident.resolution == "health_recovered"
    assert controls_after == controls_before
    assert checkpoints_after == checkpoints_before
    assert len(health_audit) == 3


async def test_health_evaluation_time_must_advance_without_writing_a_partial_result(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )
    async with uow_factory.begin() as uow:
        await uow.platform.activate_run(
            RUN_ONE,
            command_id=COMMAND_ONE,
            worker_boot_id=WORKER_ONE,
            started_at=NOW + timedelta(seconds=3),
        )
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ONE,
                boot_id=OPERATIONS_BOOT,
                role=ServiceRole.OPERATIONS,
                service_id="operations-service",
            )
        )

    evaluator = CloudHealthEvaluator(
        uow_factory=uow_factory,
        snapshot_reader=SnapshotReader(),
        service_boot_id=OPERATIONS_BOOT,
    )
    checked_at = NOW + timedelta(minutes=1)
    await evaluator.evaluate(RUN_ONE, checked_at)
    with pytest.raises(ValueError, match="advance"):
        await evaluator.evaluate(RUN_ONE, checked_at)

    async with uow_factory.begin() as uow:
        count = await uow.session.scalar(select(func.count()).select_from(HealthEvaluationModel))
    assert count == 1


async def test_sentry_delivery_failure_is_deduplicated_and_recovery_is_persisted(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )

    first = await reconcile_sentry_delivery_incident(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        operations=("daily_close", "backup", "evidence"),
        delivery_confirmed=False,
        observed_at=NOW + timedelta(minutes=1),
        incident_id_factory=lambda: SENTRY_INCIDENT,
    )
    repeated = await reconcile_sentry_delivery_incident(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        operations=("backup", "evidence"),
        delivery_confirmed=False,
        observed_at=NOW + timedelta(minutes=2),
        incident_id_factory=lambda: UUID("77777777-7777-4777-8777-777777777777"),
    )
    recovered = await reconcile_sentry_delivery_incident(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        operations=("daily_close", "backup", "evidence"),
        delivery_confirmed=True,
        observed_at=NOW + timedelta(minutes=3),
        incident_id_factory=lambda: UUID("66666666-6666-4666-8666-666666666666"),
    )

    assert first is not None
    assert first.incident_id == SENTRY_INCIDENT
    assert repeated is not None
    assert repeated.incident_id == first.incident_id
    assert recovered is not None
    assert recovered.incident_id == first.incident_id
    assert recovered.status is IncidentStatus.RESOLVED
    assert recovered.resolution == "sentry_delivery_recovered"

    async with uow_factory.begin() as uow:
        incidents = tuple(
            await uow.session.scalars(
                select(IncidentModel).where(IncidentModel.component == "sentry_cron_delivery")
            )
        )
    assert len(incidents) == 1


async def test_database_health_reader_uses_one_read_only_snapshot_and_fails_missing_daily_evidence(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    async with db_engine.connect() as connection:
        database_system_identifier = str(
            await connection.scalar(
                text("SELECT system_identifier::text FROM pg_catalog.pg_control_system()")
            )
        )
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
        database_system_identifier=database_system_identifier,
    )
    operations = _service(
        run_id=RUN_ONE,
        boot_id=OPERATIONS_BOOT,
        role=ServiceRole.OPERATIONS,
        service_id="operations-service",
    )
    checked_at = datetime.now(tz=NOW.tzinfo)
    async with uow_factory.begin() as uow:
        await uow.platform.activate_run(
            RUN_ONE,
            command_id=COMMAND_ONE,
            worker_boot_id=WORKER_ONE,
            started_at=NOW + timedelta(seconds=3),
        )
        await uow.platform.register_service_instance(operations)
        await uow.platform.heartbeat_service_instance(
            boot_id=WORKER_ONE,
            sequence=1,
            heartbeat_at=checked_at - timedelta(seconds=5),
        )
        await uow.platform.heartbeat_service_instance(
            boot_id=OPERATIONS_BOOT,
            sequence=1,
            heartbeat_at=checked_at - timedelta(seconds=5),
        )
        checkpoint = WorkerCheckpoint.create(
            experiment_id=EXPERIMENT_ONE,
            worker_id=WORKER_ONE,
            checkpoint_at=checked_at - timedelta(seconds=6),
            state={"dispatch_queue_depth": 0},
        ).transition(
            WorkerStatus.RUNNING,
            checked_at - timedelta(seconds=5),
            {"dispatch_queue_depth": 0},
        )
        await uow.orchestration.record_checkpoint(checkpoint)
        await uow.workers.acquire(
            experiment_id=EXPERIMENT_ONE,
            worker_id=WORKER_ONE,
            acquired_at=checked_at - timedelta(seconds=5),
            ttl=timedelta(minutes=5),
        )
        await uow.market_data.record_cursor(
            MarketCursor.create(
                experiment_id=EXPERIMENT_ONE,
                venue="binance_futures",
                stream="continuous_kline_1m",
                symbol="BTCUSDT",
                timeframe="1m",
                event_id="health-reader-bar",
                sequence=1,
                venue_event_at=checked_at - timedelta(seconds=60),
                observed_at=checked_at - timedelta(seconds=55),
                bar_close_at=checked_at - timedelta(seconds=60),
                updated_at=checked_at - timedelta(seconds=55),
            )
        )

    await reconcile_sentry_delivery_incident(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        operations=("daily_close", "backup", "evidence"),
        delivery_confirmed=False,
        observed_at=checked_at - timedelta(seconds=1),
        incident_id_factory=lambda: SENTRY_INCIDENT,
    )

    runtime = RuntimeIdentityEvidence(
        identity=operations.identity,
        schema_revision="0022",
        database_system_identifier_sha256=hashlib.sha256(
            database_system_identifier.encode("ascii")
        ).hexdigest(),
    )
    reader = DatabaseCloudHealthSnapshotReader(
        session_factory=async_sessionmaker(db_engine, expire_on_commit=False),
        runtime_evidence=runtime,
        environment="qualification",
        sentry_delivery_confirmed=lambda: True,
    )

    assessment = evaluate_cloud_components(
        await reader.collect(RUN_ONE, checked_at),
        checked_at=checked_at,
    )

    assert assessment.failed_check_names == (
        "backup",
        "daily_close",
        "deployment_identity",
        "sentry_delivery",
        "worm_replication",
    )
    assert assessment.components["database"]["status"] == "ok"
    assert assessment.components["worker_continuity"]["status"] == "ok"
    assert assessment.components["worker_lease"]["status"] == "ok"
    assert assessment.components["ledger"]["status"] == "ok"
    assert assessment.components["required_cursors"]["status"] == "ok"
    assert assessment.components["dispatch_queue_capacity"]["status"] == "ok"
    assert assessment.components["audit_chain"]["status"] == "ok"


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.base = datetime(2026, 8, 9, 6, tzinfo=NOW.tzinfo)
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def utc_now(self) -> datetime:
        return self.base + timedelta(seconds=self.elapsed)

    async def wait(self, stop_requested: object, delay: float) -> bool:
        self.waits.append(delay)
        self.elapsed += delay
        return bool(getattr(stop_requested, "is_set")())


class Ownership(AbstractAsyncContextManager[None]):
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exc_type, exc_value, traceback
        self.exited += 1
        return None


async def test_supervisor_uses_monotonic_minutes_without_a_catch_up_burst() -> None:
    clock = FakeClock()
    ownership = Ownership()
    checked: list[datetime] = []
    supervisor: HealthSupervisor

    class Evaluator:
        async def evaluate(self, run_id: UUID, checked_at: datetime) -> object:
            assert run_id == RUN_ONE
            checked.append(checked_at)
            if len(checked) == 1:
                clock.elapsed += 130
            else:
                supervisor.request_stop()
            return object()

    supervisor = HealthSupervisor(
        evaluator=Evaluator(),
        run_id=RUN_ONE,
        ownership=ownership,
        interval_seconds=60,
        monotonic=clock.monotonic,
        utc_now=clock.utc_now,
        wait=clock.wait,
    )

    await supervisor.run()

    assert checked == [clock.base, clock.base + timedelta(seconds=190)]
    assert clock.waits == [60]
    assert ownership.entered == ownership.exited == 1


async def test_supervisor_terminal_failure_escapes_and_releases_ownership() -> None:
    ownership = Ownership()

    class Evaluator:
        async def evaluate(self, run_id: UUID, checked_at: datetime) -> object:
            del run_id, checked_at
            raise RuntimeError("health collection failed")

    supervisor = HealthSupervisor(
        evaluator=Evaluator(),
        run_id=RUN_ONE,
        ownership=ownership,
        utc_now=lambda: datetime(2026, 8, 9, 6, tzinfo=NOW.tzinfo),
    )

    with pytest.raises(RuntimeError, match="health collection failed"):
        await supervisor.run()
    assert ownership.entered == ownership.exited == 1


async def test_postgres_health_ownership_rejects_a_second_supervisor(
    db_engine: AsyncEngine,
) -> None:
    first = PostgresHealthOwnership(db_engine, run_id=RUN_ONE)
    second = PostgresHealthOwnership(db_engine, run_id=RUN_ONE)

    async with first:
        with pytest.raises(HealthSupervisorAlreadyRunning):
            async with second:
                raise AssertionError("duplicate health owner entered")
