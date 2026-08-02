from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from maais.db.models.accounts import AccountSnapshotModel, ExitPlanModel
from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.decisions import AgentEvaluationModel, DecisionCycleModel, MarketFrameModel
from maais.db.models.execution import ExecutionSensitivityModel, FillModel, OrderIntentModel
from maais.db.models.experiments import AgentVersionModel
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.models.operations import (
    DataQualityEvaluationModel,
    IncidentModel,
    MarketCursorModel,
)
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.market_data import OperationalStateConflict
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import Direction, StrategyStage
from maais.market_data.frames import CausalMinuteFrameBuilder, FrameKey
from maais.market_data.integrity.state_machine import (
    IntegrityPolicy,
    MarketIntegrityStateMachine,
)
from maais.market_data.recovery import MarketCursor
from maais.orchestration.commands import OrchestrationCommand
from maais.orchestration.results import OrchestrationDisposition
from maais.orchestration.service import OfficialOrchestrationService
from tests.unit.experiments.test_manifest import _manifest
from tests.unit.market_data.test_frame_builder import _inputs
from tests.unit.market_data.test_integrity_state_machine import _context
from tests.unit.orchestration.test_service import (
    _entry_context,
    _execution_service,
    _FeatureComputer,
    _features,
)

pytestmark = pytest.mark.integration


async def _command_in_database(
    uow_factory: UnitOfWork,
    *,
    quarantine: bool,
    with_entry_context: bool = False,
    kill_switch: bool = False,
) -> OrchestrationCommand:
    manifest = _manifest(experiment_id=UUID(int=1), schema_revision="0009")
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
    inputs = _inputs()
    bar = inputs[0]
    frame = CausalMinuteFrameBuilder().build(
        FrameKey(
            experiment_id=manifest.experiment_id,
            strategy_version_id=strategy_id,
            symbol="BTCUSDT",
            timeframe="1m",
            bar_close_at=bar.payload.bar_close_at,  # type: ignore[union-attr]
        ),
        bar,
        inputs,
    )
    context = _context(frame)
    if quarantine:
        context = replace(context, historical_bar_count=0, recent_close_returns=())
    integrity = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(context)
    return OrchestrationCommand(
        frame=frame,
        integrity=integrity,
        manifest=manifest,
        agent_version_ids=agent_ids,
        evaluated_at=context.evaluated_at,
        completed_at=context.evaluated_at,
        entry_context=(_entry_context(kill_switch=kill_switch) if with_entry_context else None),
    )


def _cursor(command: OrchestrationCommand) -> MarketCursor:
    source = command.frame.source_manifest["closed_bar"]
    if source.sequence is None:
        raise AssertionError("closed-bar fixture must be sequenced")
    return MarketCursor.create(
        experiment_id=command.manifest.experiment_id,
        venue=source.venue,
        stream=source.stream,
        symbol=command.frame.key.symbol,
        timeframe=command.frame.key.timeframe,
        event_id=source.event_id,
        sequence=source.sequence,
        venue_event_at=source.venue_event_at,
        observed_at=source.observed_at,
        bar_close_at=command.frame.bar.bar_close_at,
        updated_at=command.completed_at,
    )


async def test_quarantine_outcome_cursor_incident_and_events_commit_atomically(
    uow_factory: UnitOfWork,
) -> None:
    command = await _command_in_database(uow_factory, quarantine=True)
    outcome = await OfficialOrchestrationService(_FeatureComputer(_features())).process(command)
    cursor = _cursor(command)

    async with uow_factory.begin() as uow:
        first = await uow.orchestration.record_outcome(
            outcome,
            integrity=command.integrity,
            required_checks=IntegrityPolicy.official().required_checks,
            evaluated_at=command.evaluated_at,
            cursor=cursor,
        )
    async with uow_factory.begin() as uow:
        second = await uow.orchestration.record_outcome(
            outcome,
            integrity=command.integrity,
            required_checks=IntegrityPolicy.official().required_checks,
            evaluated_at=command.evaluated_at,
            cursor=cursor,
        )
        restored = await uow.decisions.get_bundle(outcome.bundle.cycle.id)
        consistency = await verify_ledger_consistency(uow.session)

    assert first.decision.created and first.quality.created
    assert first.cursor is not None and first.cursor.created
    assert first.incident is not None and first.incident.created
    assert not second.decision.created and not second.quality.created
    assert second.cursor is not None and not second.cursor.created
    assert second.incident is not None and not second.incident.created
    assert restored.bundle == outcome.bundle
    assert consistency.ok


async def test_rejected_direction_persists_counterfactual_with_decision(
    uow_factory: UnitOfWork,
) -> None:
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
        kill_switch=True,
    )
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.disposition is OrchestrationDisposition.REJECTED

    async with uow_factory.begin() as uow:
        result = await uow.orchestration.record_outcome(
            outcome,
            integrity=command.integrity,
            required_checks=IntegrityPolicy.official().required_checks,
            evaluated_at=command.evaluated_at,
        )
        unresolved = await uow.counterfactuals.get_unresolved(command.manifest.experiment_id)
        consistency = await verify_ledger_consistency(uow.session)

    assert result.counterfactual is not None and result.counterfactual.created
    assert unresolved == (outcome.counterfactual,)
    assert consistency.ok


async def test_executed_direction_persists_fill_account_exit_and_sensitivities(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
    )
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.disposition is OrchestrationDisposition.EXECUTED

    async with uow_factory.begin() as uow:
        result = await uow.orchestration.record_outcome(
            outcome,
            integrity=command.integrity,
            required_checks=IntegrityPolicy.official().required_checks,
            evaluated_at=command.evaluated_at,
            cursor=_cursor(command),
        )
        restored_account = await uow.paper_execution.load_account(command.manifest.experiment_id)
        pending_orders = await uow.paper_execution.load_pending_orders(
            command.manifest.experiment_id
        )
        open_exit_plans = await uow.paper_execution.load_open_exit_plans(
            command.manifest.experiment_id
        )
        consistency = await verify_ledger_consistency(uow.session)

    assert result.execution is not None and result.execution.created
    assert result.sensitivity_rows_created == 3
    assert outcome.execution is not None
    assert restored_account == outcome.execution.account
    assert pending_orders == ()
    assert open_exit_plans == (outcome.execution.exit_plan,)
    assert consistency.ok
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderIntentModel)) == 1
        assert await session.scalar(select(func.count()).select_from(FillModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 1
        assert await session.scalar(select(func.count()).select_from(ExitPlanModel)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(ExecutionSensitivityModel)) == 3
        )


async def test_late_incident_conflict_rolls_back_decision_quality_and_cursor(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    command = await _command_in_database(uow_factory, quarantine=True)
    outcome = await OfficialOrchestrationService(_FeatureComputer(_features())).process(command)
    assert outcome.incident is not None
    conflicting = replace(outcome.incident, evidence={"different": "immutable evidence"})
    async with uow_factory.begin() as uow:
        await uow.incidents.record(conflicting)

    with pytest.raises(OperationalStateConflict, match="immutable identity"):
        async with uow_factory.begin() as uow:
            await uow.orchestration.record_outcome(
                outcome,
                integrity=command.integrity,
                required_checks=IntegrityPolicy.official().required_checks,
                evaluated_at=command.evaluated_at,
                cursor=_cursor(command),
            )

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(MarketFrameModel)) == 0
        assert await session.scalar(select(func.count()).select_from(DecisionCycleModel)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentEvaluationModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(DataQualityEvaluationModel)) == 0
        )
        assert await session.scalar(select(func.count()).select_from(MarketCursorModel)) == 0
        assert await session.scalar(select(func.count()).select_from(IncidentModel)) == 1
        domain_count = int(
            await session.scalar(select(func.count()).select_from(DomainEventModel)) or 0
        )
        outbox_count = int(
            await session.scalar(select(func.count()).select_from(OutboxEventModel)) or 0
        )
        assert domain_count == outbox_count == 2
        assert await session.scalar(select(func.count()).select_from(CounterfactualModel)) == 0
