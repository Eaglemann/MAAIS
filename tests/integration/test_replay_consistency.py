from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from functools import partial
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from maais.config.modes import RunMode
from maais.config.settings import get_settings
from maais.db.models.execution import OrderIntentModel
from maais.db.models.ledger import DomainEventModel, EventStreamModel, OutboxEventModel
from maais.db.replay import (
    LedgerConsistencyError,
    LedgerConsistencyReport,
    rebuild_experiment_projection,
    verify_ledger_consistency,
)
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ExperimentStatus
from maais.experiments.service import ExperimentLifecycle
from maais.operations.backups import collect_backup_metadata
from maais.operations.health import collect_configured_experiment_health
from maais.operations.operator_commands import CommandType, OperatorCommand
from maais.operations.reporting import build_configured_daily_report, build_daily_report
from maais.operations.verification import verify_ledger_with_factory
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.integration.test_paper_execution_repository import _record

pytestmark = pytest.mark.integration


async def _snapshot_consistency_probe(
    session: AsyncSession,
    *,
    first_read_complete: asyncio.Event,
    concurrent_write_complete: asyncio.Event,
) -> LedgerConsistencyReport:
    first_count = int(await session.scalar(select(func.count()).select_from(DomainEventModel)) or 0)
    first_read_complete.set()
    await asyncio.wait_for(concurrent_write_complete.wait(), timeout=5)
    second_count = int(
        await session.scalar(select(func.count()).select_from(DomainEventModel)) or 0
    )
    if second_count == first_count:
        return LedgerConsistencyReport(())
    return LedgerConsistencyReport(
        (
            LedgerConsistencyError(
                code="snapshot_changed",
                aggregate_type=None,
                aggregate_id=None,
                details=f"first_count={first_count}, second_count={second_count}",
            ),
        )
    )


async def test_consistency_report_accepts_valid_ledger(
    uow_factory: UnitOfWork,
) -> None:
    _manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
    async with uow_factory.begin() as uow:
        report = await verify_ledger_consistency(uow.session)

    assert report.ok
    assert not report.errors


async def test_operator_verification_returns_serializable_valid_result(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    _manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)

    result = await verify_ledger_with_factory(async_sessionmaker(db_engine, expire_on_commit=False))

    assert result == {"ok": True, "error_count": 0, "errors": []}


async def test_operator_verification_keeps_one_snapshot_during_concurrent_write(
    monkeypatch: pytest.MonkeyPatch,
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    _manifest, bundle = await _prepare_bundle(uow_factory)
    first_read_complete = asyncio.Event()
    concurrent_write_complete = asyncio.Event()

    monkeypatch.setattr(
        "maais.operations.verification.verify_ledger_consistency",
        partial(
            _snapshot_consistency_probe,
            first_read_complete=first_read_complete,
            concurrent_write_complete=concurrent_write_complete,
        ),
    )
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    verification_task = asyncio.create_task(verify_ledger_with_factory(factory))

    await asyncio.wait_for(first_read_complete.wait(), timeout=5)
    try:
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(bundle)
    finally:
        concurrent_write_complete.set()

    result = await verification_task

    assert result == {"ok": True, "error_count": 0, "errors": []}


async def test_health_verification_keeps_one_snapshot_during_concurrent_write(
    monkeypatch: pytest.MonkeyPatch,
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    first_read_complete = asyncio.Event()
    concurrent_write_complete = asyncio.Event()
    settings = get_settings().model_copy(update={"database_url": test_database_url})

    monkeypatch.setattr(
        "maais.operations.health.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "maais.operations.health.verify_ledger_consistency",
        partial(
            _snapshot_consistency_probe,
            first_read_complete=first_read_complete,
            concurrent_write_complete=concurrent_write_complete,
        ),
    )
    health_task = asyncio.create_task(
        collect_configured_experiment_health(
            manifest.experiment_id,
            maximum_lag=timedelta(seconds=180),
            allow_stopped=False,
            send_alert=False,
        )
    )

    await asyncio.wait_for(first_read_complete.wait(), timeout=5)
    try:
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(bundle)
    finally:
        concurrent_write_complete.set()

    result = await health_task
    checks = result["checks"]
    assert isinstance(checks, list)
    ledger_check = next(check for check in checks if check["name"] == "ledger_consistency")

    assert ledger_check == {
        "name": "ledger_consistency",
        "passed": True,
        "detail": "ledger errors=0",
    }


async def test_daily_report_keeps_one_snapshot_during_concurrent_write(
    monkeypatch: pytest.MonkeyPatch,
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory, mode=RunMode.PAPER_LIVE)
    first_read_complete = asyncio.Event()
    concurrent_write_complete = asyncio.Event()
    settings = get_settings().model_copy(update={"database_url": test_database_url})

    async def build_snapshot_probe(session, *_args, **_kwargs) -> dict[str, object]:
        result = await _snapshot_consistency_probe(
            session,
            first_read_complete=first_read_complete,
            concurrent_write_complete=concurrent_write_complete,
        )
        return {"snapshot_stable": result.ok}

    monkeypatch.setattr(
        "maais.operations.reporting.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "maais.operations.reporting.build_daily_report",
        build_snapshot_probe,
    )
    report_task = asyncio.create_task(
        build_configured_daily_report(
            manifest.experiment_id,
            date(2026, 8, 2),
            generated_at=datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc),
        )
    )

    await asyncio.wait_for(first_read_complete.wait(), timeout=5)
    try:
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(bundle)
    finally:
        concurrent_write_complete.set()

    report = await report_task

    assert report == {"snapshot_stable": True}


async def test_backup_metadata_keeps_one_snapshot_during_concurrent_write(
    monkeypatch: pytest.MonkeyPatch,
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    _manifest, bundle = await _prepare_bundle(uow_factory)
    first_read_complete = asyncio.Event()
    concurrent_write_complete = asyncio.Event()

    monkeypatch.setattr(
        "maais.operations.backups.verify_ledger_consistency",
        partial(
            _snapshot_consistency_probe,
            first_read_complete=first_read_complete,
            concurrent_write_complete=concurrent_write_complete,
        ),
    )
    backup_task = asyncio.create_task(collect_backup_metadata(test_database_url))

    await asyncio.wait_for(first_read_complete.wait(), timeout=5)
    try:
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(bundle)
    finally:
        concurrent_write_complete.set()

    metadata = await backup_task

    assert metadata.ledger == {"ok": True, "error_count": 0, "errors": []}


async def test_daily_report_reconciles_complete_decision_lineage(
    uow_factory: UnitOfWork,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory, mode=RunMode.PAPER_LIVE)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
        command = OperatorCommand.request(
            command_id=UUID("99999999-9999-4999-8999-999999999999"),
            experiment_id=manifest.experiment_id,
            command_type=CommandType.PAUSE,
            idempotency_key="daily-report-pause-0001",
            actor="local_operator",
            reason="inspect an unexpected concentration before continuing",
            payload={"source": "mission_control"},
            confirmation="CONFIRM PAUSE",
            requested_at=datetime(2026, 8, 2, 12, 1, tzinfo=timezone.utc),
        )
        await uow.commands.enqueue(command)
        await uow.commands.claim_next(
            manifest.experiment_id,
            worker_id="paper_worker:daily-report",
            accepted_at=datetime(2026, 8, 2, 12, 1, 1, tzinfo=timezone.utc),
        )
        await uow.commands.complete(
            command.command_id,
            worker_id="paper_worker:daily-report",
            completed_at=datetime(2026, 8, 2, 12, 1, 2, tzinfo=timezone.utc),
            result={
                "experiment_status": "paused",
                "kill_switch_active": True,
                "control_version": 2,
            },
        )
    generated_at = datetime(2026, 8, 3, 0, 5, tzinfo=timezone.utc)

    async with uow_factory.begin() as uow:
        report = await build_daily_report(
            uow.session,
            manifest.experiment_id,
            date(2026, 8, 2),
            generated_at=generated_at,
        )

    assert report["decisions"] == {
        "total": 1,
        "by_status": {"completed": 1},
        "by_disposition": {"approved": 1},
        "by_direction": {"long": 1},
        "by_reason": {"accepted": 1},
        "by_symbol": {"BTCUSDT": 1},
        "by_regime": {"trending": 1},
    }
    assert report["decision_index"] == [
        {
            "content_hash": bundle.bundle_hash,
            "cycle_at": "2026-08-02T12:00:00Z",
            "direction": "long",
            "disposition": "approved",
            "id": str(bundle.cycle.id),
            "market_frame_id": str(bundle.cycle.market_frame_id),
            "reason_code": "accepted",
            "regime": "trending",
            "status": "completed",
            "symbol": "BTCUSDT",
            "timeframe": "1m",
        }
    ]
    assert report["agents"]["evaluations"] == 8  # type: ignore[index]
    assert report["gates"]["evaluations"] == 2  # type: ignore[index]
    assert report["execution"]["proposals"] == 1  # type: ignore[index]
    assert report["account"]["starting_equity"] == "10000"  # type: ignore[index]
    assert report["account"]["ending_equity"] == "10000"  # type: ignore[index]
    assert report["reconciliation"]["ledger_ok"] is True  # type: ignore[index]
    assert len(report["reconciliation"]["report_hash"]) == 64  # type: ignore[index]
    assert report["report_schema_version"] == 2
    assert report["experiment"]["started_at"] is None  # type: ignore[index]
    assert report["operator_actions"] == {
        "events": 3,
        "requests": 1,
        "rejections": 0,
        "recoveries": 0,
        "by_event_type": {
            "operator_command.accepted": 1,
            "operator_command.completed": 1,
            "operator_command.requested": 1,
        },
        "by_command_type": {"pause": 3},
        "by_status": {"accepted": 1, "completed": 1, "requested": 1},
    }
    action_trail = report["operator_action_index"]  # type: ignore[assignment]
    assert [item["event_type"] for item in action_trail] == [  # type: ignore[index]
        "operator_command.requested",
        "operator_command.accepted",
        "operator_command.completed",
    ]
    terminal = action_trail[-1]  # type: ignore[index]
    assert terminal["reason"] == "inspect an unexpected concentration before continuing"
    assert terminal["accepted_by"] == "paper_worker:daily-report"
    assert terminal["result"] == {
        "control_version": 2,
        "experiment_status": "paused",
        "kill_switch_active": True,
    }


async def test_backup_inventory_reconciles_database_before_dump(
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    _manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)

    metadata = await collect_backup_metadata(test_database_url)

    assert metadata.database_name == "maais_test"
    assert metadata.schema_revision == "0017"
    assert metadata.table_counts["decision_cycles"] == 1
    assert metadata.table_counts["agent_evaluations"] == 8
    assert metadata.ledger == {"ok": True, "error_count": 0, "errors": []}


async def test_consistency_report_accepts_reconciled_paper_account(
    uow_factory: UnitOfWork,
) -> None:
    record = await _record(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(record)
        report = await verify_ledger_consistency(uow.session)

    assert report.ok


async def test_consistency_report_finds_paper_order_projection_damage(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    record = await _record(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(record)

    factory = async_sessionmaker(db_engine)
    async with factory.begin() as session:
        await session.execute(
            update(OrderIntentModel)
            .where(OrderIntentModel.id == record.order.order_id)
            .values(version=record.order.version + 1)
        )
    async with factory() as session:
        report = await verify_ledger_consistency(session)

    assert not report.ok
    assert any(error.code == "order_projection_mismatch" for error in report.errors)


async def test_consistency_report_finds_stream_and_outbox_damage(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    _manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)

    factory = async_sessionmaker(db_engine)
    async with factory.begin() as session:
        stream = await session.scalar(
            select(EventStreamModel).where(
                EventStreamModel.aggregate_type == "decision_cycle",
                EventStreamModel.aggregate_id == bundle.cycle.id,
            )
        )
        assert stream is not None
        await session.execute(
            update(EventStreamModel)
            .where(EventStreamModel.id == stream.id)
            .values(current_version=stream.current_version + 1)
        )
        outbox_id = await session.scalar(select(OutboxEventModel.id).limit(1))
        assert outbox_id is not None
        await session.execute(delete(OutboxEventModel).where(OutboxEventModel.id == outbox_id))

    async with factory() as session:
        report = await verify_ledger_consistency(session)

    assert not report.ok
    assert any(error.code == "stream_gap" for error in report.errors)
    assert any(error.code == "missing_outbox" for error in report.errors)


async def test_experiment_projection_rebuild_matches_lifecycle(
    uow_factory: UnitOfWork,
) -> None:
    manifest, _bundle = await _prepare_bundle(uow_factory)
    now_values = iter(
        (
            datetime(2026, 8, 2, 13, tzinfo=timezone.utc),
            datetime(2026, 8, 2, 14, tzinfo=timezone.utc),
            datetime(2026, 8, 2, 15, tzinfo=timezone.utc),
            datetime(2026, 8, 2, 16, tzinfo=timezone.utc),
        )
    )
    status = ExperimentStatus.CREATED
    version = 1
    for command in ("start", "pause", "resume"):
        lifecycle = ExperimentLifecycle(manifest, status, version, now=lambda: next(now_values))
        transition = getattr(lifecycle, command)()
        async with uow_factory.begin() as uow:
            await uow.experiments.transition(manifest, transition)
        status = transition.status
        version += 1
    lifecycle = ExperimentLifecycle(manifest, status, version, now=lambda: next(now_values))
    transition = lifecycle.fail("simulated terminal failure")
    async with uow_factory.begin() as uow:
        await uow.experiments.transition(manifest, transition)

    async with uow_factory.begin() as uow:
        rebuilt = await rebuild_experiment_projection(uow.session, manifest.experiment_id)
        report = await verify_ledger_consistency(uow.session)

    assert rebuilt.status is ExperimentStatus.FAILED
    assert rebuilt.failure_reason == "simulated terminal failure"
    assert rebuilt.started_at == datetime(2026, 8, 2, 13, tzinfo=timezone.utc)
    assert rebuilt.ended_at == datetime(2026, 8, 2, 16, tzinfo=timezone.utc)
    assert report.ok
