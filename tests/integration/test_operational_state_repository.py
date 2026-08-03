from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import CheckConstraint, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from maais.db.connection import Base
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.models.operations import (
    IncidentModel,
    MarketCursorModel,
    MarketRecoveryRunModel,
    TradingControlModel,
    WorkerLeaseModel,
)
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.market_data import OperationalStateConflict
from maais.db.repositories.workers import (
    WorkerLeaseConflict,
    WorkerLeaseExpired,
)
from maais.db.unit_of_work import UnitOfWork
from maais.market_data.integrity.state_machine import (
    IntegrityCheck,
    IntegrityPolicy,
    MarketIntegrityStateMachine,
)
from maais.market_data.recovery import GapRange, MarketCursor, RecoveryState
from maais.operations.incident_management import IncidentAction, apply_incident_action
from maais.operations.incidents import IncidentSeverity, IncidentState
from maais.orchestration.checkpoints import (
    WorkerCheckpoint,
    WorkerLeaseStatus,
    WorkerStatus,
)
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.unit.experiments.test_manifest import _manifest
from tests.unit.market_data.test_integrity_state_machine import _context, _frame

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


async def _manifest_in_database(uow_factory: UnitOfWork):
    manifest = _manifest(schema_revision="0009")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    return manifest


def _cursor(experiment_id: UUID) -> MarketCursor:
    return MarketCursor.create(
        experiment_id=experiment_id,
        venue="binance_futures",
        stream="continuous_kline_1m",
        symbol="BTCUSDT",
        timeframe="1m",
        event_id="bar-100",
        sequence=100,
        venue_event_at=NOW,
        observed_at=NOW + timedelta(milliseconds=100),
        bar_close_at=NOW,
        updated_at=NOW + timedelta(milliseconds=100),
    )


def _recovery(experiment_id: UUID) -> RecoveryState:
    gap = GapRange(
        experiment_id=experiment_id,
        venue="binance_futures",
        stream="continuous_kline_1m",
        symbol="BTCUSDT",
        timeframe="1m",
        start_sequence=101,
        end_sequence_exclusive=103,
        start_open_at=NOW,
        end_open_at_exclusive=NOW + timedelta(minutes=2),
        interval=timedelta(minutes=1),
    )
    return RecoveryState.create(
        recovery_id=UUID(int=701),
        experiment_id=experiment_id,
        gap=gap,
        started_at=NOW + timedelta(seconds=1),
    )


def _incident(experiment_id: UUID) -> IncidentState:
    return IncidentState.create(
        incident_id=UUID(int=801),
        experiment_id=experiment_id,
        deduplication_key="market:BTCUSDT:closed_bar_gap",
        severity=IncidentSeverity.ERROR,
        component="market_data",
        reason_code="closed_bar_gap",
        evidence={"expected_open_at": NOW, "actual_open_at": NOW + timedelta(minutes=2)},
        requires_operator_review=False,
        detected_at=NOW + timedelta(seconds=1),
    )


async def test_operational_schema_matches_models(db_connection: AsyncConnection) -> None:
    tables = (
        "market_cursors",
        "data_quality_evaluations",
        "market_recovery_runs",
        "incidents",
        "worker_checkpoints",
        "worker_leases",
        "trading_controls",
        "operator_commands",
    )

    def compare(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        for table_name in tables:
            table = Base.metadata.tables[table_name]
            assert {column["name"] for column in inspector.get_columns(table_name)} == {
                column.name for column in table.columns
            }
            assert set(inspector.get_pk_constraint(table_name)["constrained_columns"]) == {
                column.name for column in table.primary_key.columns
            }
            assert {
                tuple(item["constrained_columns"])
                for item in inspector.get_foreign_keys(table_name)
            } == {tuple(item.column_keys) for item in table.foreign_key_constraints}
            assert {item["name"] for item in inspector.get_check_constraints(table_name)} == {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }

    await db_connection.run_sync(compare)


async def test_cursor_recovery_and_incident_commit_restore_and_emit_events(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    cursor = _cursor(manifest.experiment_id)
    recovery = _recovery(manifest.experiment_id)
    incident = _incident(manifest.experiment_id)

    async with uow_factory.begin() as uow:
        assert (await uow.market_data.record_cursor(cursor)).created
        assert (await uow.market_data.record_recovery(recovery)).created
        assert (await uow.incidents.record(incident)).created

    async with uow_factory.begin() as uow:
        restored_cursor = await uow.market_data.get_cursor(
            manifest.experiment_id,
            cursor.venue,
            cursor.stream,
            cursor.symbol,
            cursor.timeframe,
        )
        restored_recovery = await uow.market_data.get_recovery(recovery.recovery_id)
        restored_incident = await uow.incidents.get(incident.incident_id)
        consistency = await verify_ledger_consistency(uow.session)

    assert restored_cursor == cursor
    assert restored_recovery == recovery
    assert restored_incident == incident
    assert consistency.ok

    failed = recovery.fail("manual_backfill_required", NOW + timedelta(seconds=2))
    async with uow_factory.begin() as uow:
        await uow.market_data.record_recovery(failed)
        assert await uow.market_data.get_active_recoveries(manifest.experiment_id) == ()
        assert await uow.market_data.get_blocking_recoveries(manifest.experiment_id) == (failed,)
        assert (await verify_ledger_consistency(uow.session)).ok

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(MarketCursorModel)) == 1
        assert await session.scalar(select(func.count()).select_from(MarketRecoveryRunModel)) == 1
        assert await session.scalar(select(func.count()).select_from(IncidentModel)) == 1
        domain_count = int(
            await session.scalar(select(func.count()).select_from(DomainEventModel)) or 0
        )
        outbox_count = int(
            await session.scalar(select(func.count()).select_from(OutboxEventModel)) or 0
        )
        assert domain_count == 5  # experiment, three aggregates, and recovery failure
        assert outbox_count == domain_count


async def test_all_experiment_cursors_restore_in_stable_identity_order(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    btc = _cursor(manifest.experiment_id)
    eth = replace(
        btc,
        symbol="ETHUSDT",
        event_id="eth-bar-100",
    )
    async with uow_factory.begin() as uow:
        await uow.market_data.record_cursor(eth)
        await uow.market_data.record_cursor(btc)

    async with uow_factory.begin() as uow:
        restored = await uow.market_data.load_cursors(manifest.experiment_id)

    assert restored == (btc, eth)


async def test_trading_control_halt_is_persistent_event_backed_and_idempotent(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    async with uow_factory.begin() as uow:
        initialized = await uow.controls.initialize(
            manifest.experiment_id,
            initialized_at=NOW,
        )
    async with uow_factory.begin() as uow:
        repeated = await uow.controls.initialize(
            manifest.experiment_id,
            initialized_at=NOW + timedelta(seconds=1),
        )
        halted = await uow.controls.halt(
            manifest.experiment_id,
            reason="operator_emergency_halt",
            halted_at=NOW + timedelta(seconds=2),
            actor="local_operator",
        )
    async with uow_factory.begin() as uow:
        idempotent = await uow.controls.halt(
            manifest.experiment_id,
            reason="operator_emergency_halt",
            halted_at=NOW + timedelta(seconds=3),
            actor="local_operator",
        )
        restored = await uow.controls.current(manifest.experiment_id)

    assert not initialized.kill_switch_active
    assert repeated == initialized
    assert halted.kill_switch_active
    assert halted.reason == "operator_emergency_halt"
    assert halted.version == 2
    assert halted.changed_by == "local_operator"
    assert idempotent == halted
    assert restored == halted
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(TradingControlModel)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DomainEventModel)
                .where(DomainEventModel.aggregate_type == "trading_control")
            )
            == 2
        )


async def test_operational_transitions_are_contiguous_idempotent_and_conflict_checked(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    cursor = _cursor(manifest.experiment_id)
    recovery = _recovery(manifest.experiment_id)
    incident = _incident(manifest.experiment_id)
    async with uow_factory.begin() as uow:
        await uow.market_data.record_cursor(cursor)
        await uow.market_data.record_recovery(recovery)
        await uow.incidents.record(incident)

    backfilling = recovery.begin(NOW + timedelta(seconds=2))
    acknowledged = incident.acknowledge("operator", NOW + timedelta(seconds=2))
    async with uow_factory.begin() as uow:
        assert not (await uow.market_data.record_cursor(cursor)).created
        assert not (await uow.market_data.record_recovery(backfilling)).created
        assert not (await uow.incidents.record(acknowledged)).created

    async with uow_factory.begin() as uow:
        assert await uow.market_data.get_recovery(recovery.recovery_id) == backfilling
        assert await uow.incidents.get(incident.incident_id) == acknowledged

    changed = replace(acknowledged, severity=IncidentSeverity.CRITICAL)
    with pytest.raises(OperationalStateConflict, match="immutable identity"):
        async with uow_factory.begin() as uow:
            await uow.incidents.record(changed)

    alternate_event = replace(acknowledged.events[-1], payload={"actor": "alternate"})
    alternate_history = replace(
        acknowledged,
        acknowledged_by="alternate",
        events=(*acknowledged.events[:-1], alternate_event),
    )
    with pytest.raises(OperationalStateConflict, match="version has different content"):
        async with uow_factory.begin() as uow:
            await uow.incidents.record(alternate_history)


async def test_operator_incident_actions_are_experiment_scoped_and_audited(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    incident = _incident(manifest.experiment_id)
    async with uow_factory.begin() as uow:
        await uow.incidents.record(incident)

    with pytest.raises(LookupError, match="does not belong"):
        await apply_incident_action(
            uow_factory,
            experiment_id=UUID(int=999),
            incident_id=incident.incident_id,
            action=IncidentAction.ACKNOWLEDGE,
            actor="denis",
            occurred_at=NOW + timedelta(seconds=2),
        )

    acknowledged = await apply_incident_action(
        uow_factory,
        experiment_id=manifest.experiment_id,
        incident_id=incident.incident_id,
        action=IncidentAction.ACKNOWLEDGE,
        actor="denis",
        occurred_at=NOW + timedelta(seconds=2),
    )
    resolved = await apply_incident_action(
        uow_factory,
        experiment_id=manifest.experiment_id,
        incident_id=incident.incident_id,
        action=IncidentAction.RESOLVE,
        actor="denis",
        resolution="transient venue timestamp skew reviewed against the next cycle",
        operator_confirmed=True,
        occurred_at=NOW + timedelta(seconds=3),
    )

    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["version"] == 2
    assert acknowledged["event_type"] == "incident.acknowledged"
    assert resolved["status"] == "resolved"
    assert resolved["version"] == 3
    assert resolved["event_type"] == "incident.resolved"
    assert resolved["operator_confirmed"] is True
    async with uow_factory.begin() as uow:
        restored = await uow.incidents.get(incident.incident_id)
    assert restored.status.value == "resolved"
    assert [event.sequence for event in restored.events] == [1, 2, 3]


async def test_worker_checkpoint_uses_optimistic_versions_and_restores(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    checkpoint = WorkerCheckpoint.create(
        experiment_id=manifest.experiment_id,
        worker_id=UUID(int=901),
        checkpoint_at=NOW,
        state={"cursor_count": 0},
    )
    running = checkpoint.transition(
        WorkerStatus.RUNNING,
        NOW + timedelta(seconds=1),
        {"cursor_count": 1},
    )
    async with uow_factory.begin() as uow:
        assert (await uow.orchestration.record_checkpoint(checkpoint)).created
    async with uow_factory.begin() as uow:
        assert not (await uow.orchestration.record_checkpoint(running)).created
        assert await uow.orchestration.get_checkpoint(manifest.experiment_id) == running

    wrong_worker = running.restart(
        worker_id=UUID(int=902),
        checkpoint_at=NOW + timedelta(seconds=2),
        state={"cursor_count": 1},
    )
    with pytest.raises(OperationalStateConflict, match="active worker lease"):
        async with uow_factory.begin() as uow:
            await uow.orchestration.record_checkpoint(wrong_worker)


async def test_worker_lease_blocks_competitors_and_audits_expiry_takeover(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    first_worker = UUID(int=911)
    second_worker = UUID(int=912)
    ttl = timedelta(seconds=30)

    async with uow_factory.begin() as uow:
        acquired = await uow.workers.acquire(
            experiment_id=manifest.experiment_id,
            worker_id=first_worker,
            acquired_at=NOW,
            ttl=ttl,
        )
    assert acquired.epoch == 1
    assert acquired.valid_at(NOW + timedelta(seconds=29))

    with pytest.raises(WorkerLeaseConflict, match="another worker"):
        async with uow_factory.begin() as uow:
            await uow.workers.acquire(
                experiment_id=manifest.experiment_id,
                worker_id=second_worker,
                acquired_at=NOW + timedelta(seconds=1),
                ttl=ttl,
            )

    async with uow_factory.begin() as uow:
        renewed = await uow.workers.renew(
            experiment_id=manifest.experiment_id,
            worker_id=first_worker,
            heartbeat_at=NOW + timedelta(seconds=10),
            ttl=ttl,
        )
    assert renewed.expires_at == NOW + timedelta(seconds=40)

    with pytest.raises(WorkerLeaseExpired):
        async with uow_factory.begin() as uow:
            await uow.workers.renew(
                experiment_id=manifest.experiment_id,
                worker_id=first_worker,
                heartbeat_at=NOW + timedelta(seconds=40),
                ttl=ttl,
            )

    async with uow_factory.begin() as uow:
        taken = await uow.workers.acquire(
            experiment_id=manifest.experiment_id,
            worker_id=second_worker,
            acquired_at=NOW + timedelta(seconds=40),
            ttl=ttl,
        )
        released = await uow.workers.release(
            experiment_id=manifest.experiment_id,
            worker_id=second_worker,
            released_at=NOW + timedelta(seconds=41),
        )
        assert (await verify_ledger_consistency(uow.session)).ok

    assert taken.epoch == 2
    assert taken.worker_id == second_worker
    assert released.status is WorkerLeaseStatus.RELEASED
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WorkerLeaseModel)) == 1
        event_types = tuple(
            await session.scalars(
                select(DomainEventModel.event_type)
                .where(
                    DomainEventModel.aggregate_type == "worker_lease",
                    DomainEventModel.aggregate_id == manifest.experiment_id,
                )
                .order_by(DomainEventModel.stream_version)
            )
        )
    assert event_types == (
        "worker_lease.acquired",
        "worker_lease.taken_over",
        "worker_lease.released",
    )


async def test_expired_lease_takeover_can_restart_existing_checkpoint(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    first_worker = UUID(int=921)
    second_worker = UUID(int=922)
    checkpoint = WorkerCheckpoint.create(
        experiment_id=manifest.experiment_id,
        worker_id=first_worker,
        checkpoint_at=NOW,
        state={"cursor_count": 0, "lease_epoch": 1},
    ).transition(
        WorkerStatus.RUNNING,
        NOW + timedelta(seconds=1),
        {"cursor_count": 1, "lease_epoch": 1},
    )
    async with uow_factory.begin() as uow:
        await uow.workers.acquire(
            experiment_id=manifest.experiment_id,
            worker_id=first_worker,
            acquired_at=NOW,
            ttl=timedelta(seconds=30),
        )
        await uow.orchestration.record_checkpoint(checkpoint)

    restarted = checkpoint.restart(
        worker_id=second_worker,
        checkpoint_at=NOW + timedelta(seconds=30),
        state={"cursor_count": 1, "lease_epoch": 2},
    )
    async with uow_factory.begin() as uow:
        await uow.workers.acquire(
            experiment_id=manifest.experiment_id,
            worker_id=second_worker,
            acquired_at=NOW + timedelta(seconds=30),
            ttl=timedelta(seconds=30),
        )
        await uow.orchestration.record_checkpoint(restarted)

    async with uow_factory.begin() as uow:
        assert await uow.orchestration.get_checkpoint(manifest.experiment_id) == restarted
        assert (await verify_ledger_consistency(uow.session)).ok


async def test_quality_rows_are_complete_idempotent_and_event_backed(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    _manifest_value, bundle = await _prepare_bundle(uow_factory)
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(_frame())
    )
    adapted_bundle = replace(
        bundle,
        market_frame=replace(bundle.market_frame, id=assessment.frame_id),
        cycle=replace(bundle.cycle, market_frame_id=assessment.frame_id),
    )
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(adapted_bundle)
        first = await uow.market_data.record_quality(
            assessment,
            evaluated_at=adapted_bundle.market_frame.observed_at,
            required_checks=IntegrityPolicy.official().required_checks,
        )
    async with uow_factory.begin() as uow:
        second = await uow.market_data.record_quality(
            assessment,
            evaluated_at=adapted_bundle.market_frame.observed_at,
            required_checks=IntegrityPolicy.official().required_checks,
        )

    assert first.created
    assert not second.created
    assert first.row_count == len(IntegrityCheck)
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        from maais.db.models.operations import DataQualityEvaluationModel

        assert await session.scalar(
            select(func.count()).select_from(DataQualityEvaluationModel)
        ) == len(IntegrityCheck)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DomainEventModel)
                .where(
                    DomainEventModel.aggregate_type == "market_quality",
                    DomainEventModel.aggregate_id == assessment.frame_id,
                )
            )
            == 1
        )


async def test_operational_transaction_rolls_back_projections_events_and_outbox(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    with pytest.raises(RuntimeError, match="rollback"):
        async with uow_factory.begin() as uow:
            await uow.market_data.record_cursor(_cursor(manifest.experiment_id))
            await uow.market_data.record_recovery(_recovery(manifest.experiment_id))
            await uow.incidents.record(_incident(manifest.experiment_id))
            raise RuntimeError("rollback")

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(MarketCursorModel)) == 0
        assert await session.scalar(select(func.count()).select_from(MarketRecoveryRunModel)) == 0
        assert await session.scalar(select(func.count()).select_from(IncidentModel)) == 0
        # Only the separately committed experiment creation remains.
        assert await session.scalar(select(func.count()).select_from(DomainEventModel)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxEventModel)) == 1


async def test_concurrent_identical_incident_has_one_creator(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    incident = _incident(manifest.experiment_id)

    async def persist():
        async with uow_factory.begin() as uow:
            return await uow.incidents.record(incident)

    first, second = await asyncio.gather(persist(), persist())

    assert sorted((first.created, second.created)) == [False, True]
    async with uow_factory.begin() as uow:
        assert await uow.incidents.get(incident.incident_id) == incident
        assert (await verify_ledger_consistency(uow.session)).ok
