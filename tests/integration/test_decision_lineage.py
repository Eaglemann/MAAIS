from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from maais.config.modes import RunMode
from maais.db.connection import Base
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    GateEvaluationModel,
)
from maais.db.models.experiments import AgentVersionModel
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.repositories.decisions import DecisionIdentityConflict
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ReasonCode, StrategyStage
from tests.unit.decisions.test_bundle import _valid_bundle
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

_DECISION_TABLES = (
    "market_frames",
    "decision_cycles",
    "agent_evaluations",
    "decision_summaries",
    "gate_evaluations",
    "trade_proposals",
)


async def test_decision_schema_matches_model_columns_and_keys(
    db_connection: AsyncConnection,
) -> None:
    def compare(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        for table_name in _DECISION_TABLES:
            table = Base.metadata.tables[table_name]
            migrated_columns = {
                column["name"]: column for column in inspector.get_columns(table_name)
            }
            assert set(migrated_columns) == {column.name for column in table.columns}
            for column in table.columns:
                assert migrated_columns[column.name]["nullable"] == column.nullable
            migrated_pk = set(inspector.get_pk_constraint(table_name)["constrained_columns"])
            model_pk = {column.name for column in table.primary_key.columns}
            assert migrated_pk == model_pk
            migrated_fks = {
                tuple(item["constrained_columns"])
                for item in inspector.get_foreign_keys(table_name)
            }
            model_fks = {
                tuple(constraint.column_keys) for constraint in table.foreign_key_constraints
            }
            assert migrated_fks == model_fks

    await db_connection.run_sync(compare)


async def _prepare_bundle(
    uow_factory: UnitOfWork,
    *,
    mode: RunMode = RunMode.REPLAY,
):
    manifest = _manifest(schema_revision="0006", mode=mode)
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        strategy_id = await uow.experiments.register_strategy_version(
            strategy_key="maais_primary",
            version="1.0.0",
            stage=StrategyStage.SIMULATION,
            implementation_hash="b" * 64,
            parameters={"timeframe": "1m"},
        )
        rows = (
            await uow.session.execute(select(AgentVersionModel.agent_name, AgentVersionModel.id))
        ).all()
    agent_ids = {name: version_id for name, version_id in rows}
    return manifest, _valid_bundle(
        experiment_id=manifest.experiment_id,
        strategy_version_id=strategy_id,
        agent_version_ids=agent_ids,
    )


async def test_record_bundle_is_complete_and_emits_atomic_events(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        result = await uow.decisions.record_bundle(bundle)

    assert result.created
    assert result.content_hash == bundle.bundle_hash
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AgentEvaluationModel)
                .where(AgentEvaluationModel.decision_cycle_id == result.decision_cycle_id)
            )
            == 8
        )
        assert await session.scalar(
            select(func.count())
            .select_from(GateEvaluationModel)
            .where(GateEvaluationModel.decision_cycle_id == result.decision_cycle_id)
        ) == len(bundle.gates)
        decision_event_count = await session.scalar(
            select(func.count())
            .select_from(DomainEventModel)
            .where(
                DomainEventModel.aggregate_type == "decision_cycle",
                DomainEventModel.aggregate_id == result.decision_cycle_id,
            )
        )
        assert decision_event_count == 10 + len(bundle.gates)
        total_decision_outbox = await session.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .join(DomainEventModel, DomainEventModel.id == OutboxEventModel.domain_event_id)
            .where(DomainEventModel.aggregate_id == result.decision_cycle_id)
        )
        assert total_decision_outbox == decision_event_count

    async with uow_factory.begin() as uow:
        view = await uow.decisions.get_bundle(result.decision_cycle_id)
    assert view.bundle == bundle
    assert view.bundle.bundle_hash == bundle.bundle_hash
    assert view.config_hash == manifest.config_hash
    assert view.manifest_hash == manifest.manifest_hash


async def test_identical_retry_is_idempotent_but_changed_retry_conflicts(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    _manifest_value, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        first = await uow.decisions.record_bundle(bundle)
    async with uow_factory.begin() as uow:
        second = await uow.decisions.record_bundle(bundle)

    assert first.created
    assert not second.created
    assert second.decision_cycle_id == first.decision_cycle_id
    changed = replace(
        bundle,
        cycle=replace(bundle.cycle, reason_code=ReasonCode.ALPHA_FAILED),
    )
    with pytest.raises(DecisionIdentityConflict):
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(changed)

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DecisionCycleModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AgentEvaluationModel)) == 8


async def test_concurrent_identical_retry_creates_one_complete_bundle(
    uow_factory: UnitOfWork,
) -> None:
    _manifest_value, bundle = await _prepare_bundle(uow_factory)

    async def record():
        async with uow_factory.begin() as uow:
            return await uow.decisions.record_bundle(bundle)

    results = await asyncio.gather(record(), record())

    assert sorted(result.created for result in results) == [False, True]
    assert results[0].decision_cycle_id == results[1].decision_cycle_id
