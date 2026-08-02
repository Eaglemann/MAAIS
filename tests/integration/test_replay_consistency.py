from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from maais.db.models.ledger import EventStreamModel, OutboxEventModel
from maais.db.replay import rebuild_experiment_projection, verify_ledger_consistency
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ExperimentStatus
from maais.experiments.service import ExperimentLifecycle
from tests.integration.test_decision_lineage import _prepare_bundle

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
