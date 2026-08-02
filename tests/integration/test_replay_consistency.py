from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from maais.config.modes import RunMode
from maais.db.models.execution import OrderIntentModel
from maais.db.models.ledger import EventStreamModel, OutboxEventModel
from maais.db.replay import rebuild_experiment_projection, verify_ledger_consistency
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ExperimentStatus
from maais.experiments.service import ExperimentLifecycle
from maais.operations.reporting import build_daily_report
from maais.operations.verification import verify_ledger_with_factory
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.integration.test_paper_execution_repository import _record

pytestmark = pytest.mark.integration


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


async def test_daily_report_reconciles_complete_decision_lineage(
    uow_factory: UnitOfWork,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory, mode=RunMode.PAPER_LIVE)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
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
