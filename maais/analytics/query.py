from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maais.analytics.research import (
    AnalyticsBar,
    AnalyticsCounterfactual,
    AnalyticsFill,
    AnalyticsSensitivity,
    AnalyticsSnapshot,
    build_research_analytics,
)
from maais.db.models.accounts import (
    AccountSnapshotModel,
    ExitPlanModel,
    PositionLotModel,
    PositionModel,
)
from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    DecisionSummaryModel,
    MarketFrameModel,
    TradeProposalModel,
)
from maais.db.models.execution import (
    ExecutionSensitivityModel,
    FillModel,
    OrderIntentModel,
)
from maais.db.models.experiments import AgentVersionModel, ExperimentModel


@dataclass(frozen=True, slots=True)
class ResearchSensitivityDatum:
    id: UUID
    order_intent_id: UUID
    proposal_id: UUID
    decision_cycle_id: UUID
    symbol: str
    scenario: str
    calculated_at: datetime
    outcome: dict[str, object]


@dataclass(frozen=True, slots=True)
class ResearchDataset:
    analytics: dict[str, object]
    analytics_as_of: datetime | None
    counterfactuals: tuple[CounterfactualModel, ...]
    execution_sensitivities: tuple[ResearchSensitivityDatum, ...]


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


async def load_research_dataset(
    session: AsyncSession,
    experiment: ExperimentModel,
    *,
    cutoff: datetime | None = None,
) -> ResearchDataset:
    """Load one cutoff-consistent analytics dataset from authoritative projections."""
    experiment_id = experiment.id
    counterfactual_statement = select(CounterfactualModel).where(
        CounterfactualModel.experiment_id == experiment_id
    )
    if cutoff is not None:
        counterfactual_statement = counterfactual_statement.where(
            CounterfactualModel.created_at < cutoff
        )
    counterfactuals = (
        await session.scalars(
            counterfactual_statement.order_by(
                CounterfactualModel.created_at.desc(), CounterfactualModel.id
            )
        )
    ).all()

    sensitivity_statement = (
        select(ExecutionSensitivityModel, OrderIntentModel, TradeProposalModel)
        .join(OrderIntentModel, OrderIntentModel.id == ExecutionSensitivityModel.order_intent_id)
        .join(TradeProposalModel, TradeProposalModel.id == OrderIntentModel.proposal_id)
        .where(OrderIntentModel.experiment_id == experiment_id)
    )
    if cutoff is not None:
        sensitivity_statement = sensitivity_statement.where(
            ExecutionSensitivityModel.calculated_at < cutoff
        )
    sensitivity_rows = (
        await session.execute(
            sensitivity_statement.order_by(
                ExecutionSensitivityModel.calculated_at.desc(),
                ExecutionSensitivityModel.order_intent_id,
                ExecutionSensitivityModel.scenario,
            )
        )
    ).all()

    snapshot_statement = select(AccountSnapshotModel).where(
        AccountSnapshotModel.experiment_id == experiment_id
    )
    if cutoff is not None:
        snapshot_statement = snapshot_statement.where(AccountSnapshotModel.snapshot_at <= cutoff)
    snapshots = (
        await session.scalars(
            snapshot_statement.order_by(
                AccountSnapshotModel.snapshot_at, AccountSnapshotModel.account_version
            )
        )
    ).all()

    bar_statement = select(
        MarketFrameModel.symbol,
        MarketFrameModel.bar_close_at,
        MarketFrameModel.high,
        MarketFrameModel.low,
        MarketFrameModel.close,
    ).where(MarketFrameModel.experiment_id == experiment_id)
    if cutoff is not None:
        bar_statement = bar_statement.where(MarketFrameModel.bar_close_at < cutoff)
    bars = (
        await session.execute(
            bar_statement.order_by(MarketFrameModel.symbol, MarketFrameModel.bar_close_at)
        )
    ).all()

    execution_statement = (
        select(
            FillModel,
            OrderIntentModel,
            TradeProposalModel,
            DecisionCycleModel,
            DecisionSummaryModel,
        )
        .join(OrderIntentModel, OrderIntentModel.id == FillModel.order_intent_id)
        .join(TradeProposalModel, TradeProposalModel.id == OrderIntentModel.proposal_id)
        .join(DecisionCycleModel, DecisionCycleModel.id == TradeProposalModel.decision_cycle_id)
        .outerjoin(
            DecisionSummaryModel,
            DecisionSummaryModel.decision_cycle_id == DecisionCycleModel.id,
        )
        .where(OrderIntentModel.experiment_id == experiment_id)
    )
    if cutoff is not None:
        execution_statement = execution_statement.where(FillModel.fill_at < cutoff)
    execution_rows = (
        await session.execute(execution_statement.order_by(FillModel.fill_at, FillModel.id))
    ).all()

    cycle_ids = {proposal.decision_cycle_id for _, _, proposal, _, _ in execution_rows} | {
        row.decision_cycle_id for row in counterfactuals
    }
    agent_rows = (
        (
            await session.execute(
                select(AgentEvaluationModel, AgentVersionModel)
                .join(
                    AgentVersionModel,
                    AgentVersionModel.id == AgentEvaluationModel.agent_version_id,
                )
                .where(AgentEvaluationModel.decision_cycle_id.in_(cycle_ids))
                .order_by(
                    AgentEvaluationModel.decision_cycle_id,
                    AgentVersionModel.agent_name,
                )
            )
        ).all()
        if cycle_ids
        else []
    )
    agents_by_cycle: dict[UUID, list[tuple[AgentEvaluationModel, AgentVersionModel]]] = {}
    for evaluation, version in agent_rows:
        agents_by_cycle.setdefault(evaluation.decision_cycle_id, []).append((evaluation, version))

    def prediction_context(
        cycle_id: UUID,
        direction: str,
    ) -> tuple[tuple[str, ...], dict[str, Decimal]]:
        coalition: list[str] = []
        probabilities: dict[str, Decimal] = {}
        for evaluation, version in agents_by_cycle.get(cycle_id, ()):
            if not evaluation.enabled or not evaluation.compatible:
                continue
            if evaluation.direction == direction:
                coalition.append(version.agent_name)
                probability = evaluation.probability
            elif evaluation.direction == "neutral":
                probability = Decimal("0.5")
            else:
                probability = Decimal("1") - evaluation.probability
            probabilities[version.agent_name] = probability
        return tuple(sorted(coalition)), probabilities

    lot_position_rows = (
        await session.execute(
            select(PositionLotModel.opening_fill_id, PositionLotModel.position_id)
            .join(PositionModel, PositionModel.id == PositionLotModel.position_id)
            .where(PositionModel.experiment_id == experiment_id)
        )
    ).all()
    position_by_opening_fill = {
        opening_fill_id: position_id for opening_fill_id, position_id in lot_position_rows
    }
    exit_rows = (
        await session.execute(
            select(
                ExitPlanModel.position_id,
                ExitPlanModel.trigger_reason,
                ExitPlanModel.triggered_at,
            )
            .join(PositionModel, PositionModel.id == ExitPlanModel.position_id)
            .where(
                PositionModel.experiment_id == experiment_id,
                ExitPlanModel.trigger_reason.is_not(None),
            )
            .order_by(ExitPlanModel.triggered_at)
        )
    ).all()
    exits_by_position: dict[UUID, list[tuple[datetime, str]]] = {}
    for position_id, reason, triggered_at in exit_rows:
        if reason is not None and triggered_at is not None:
            exits_by_position.setdefault(position_id, []).append((triggered_at, reason))

    latest_opening_position: dict[str, UUID] = {}
    analytic_fills: list[AnalyticsFill] = []
    for fill, order, proposal, decision, summary in execution_rows:
        coalition, probabilities = prediction_context(
            proposal.decision_cycle_id,
            proposal.direction,
        )
        exit_reason = None
        if order.position_effect == "open":
            position_id = position_by_opening_fill.get(fill.id)
            if position_id is not None:
                latest_opening_position[order.symbol] = position_id
        else:
            position_id = latest_opening_position.get(order.symbol)
            candidates = exits_by_position.get(position_id, ()) if position_id else ()
            exit_reason = next(
                (
                    reason
                    for triggered_at, reason in reversed(candidates)
                    if triggered_at <= fill.fill_at
                ),
                None,
            )
        analytic_fills.append(
            AnalyticsFill(
                id=fill.id,
                symbol=order.symbol,
                side=order.side,
                position_effect=order.position_effect,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                fill_at=fill.fill_at,
                direction=proposal.direction,
                decision_cycle_id=proposal.decision_cycle_id,
                strategy_version_id=decision.strategy_version_id,
                regime=decision.regime,
                risk_at_stop=proposal.risk_at_stop,
                approved_quantity=proposal.approved_quantity,
                consensus_probability=(
                    summary.consensus_probability if summary is not None else None
                ),
                coalition=coalition,
                agent_probabilities=probabilities,
                exit_reason=exit_reason,
            )
        )

    summary_by_cycle = {
        decision.id: summary for _, _, _, decision, summary in execution_rows if summary is not None
    }
    missing_summary_ids = {
        row.decision_cycle_id
        for row in counterfactuals
        if row.decision_cycle_id not in summary_by_cycle
    }
    if missing_summary_ids:
        missing_summaries = (
            await session.scalars(
                select(DecisionSummaryModel).where(
                    DecisionSummaryModel.decision_cycle_id.in_(missing_summary_ids)
                )
            )
        ).all()
        summary_by_cycle.update(
            {summary.decision_cycle_id: summary for summary in missing_summaries}
        )

    analytic_counterfactuals: list[AnalyticsCounterfactual] = []
    for row in counterfactuals:
        _, probabilities = prediction_context(row.decision_cycle_id, row.direction)
        summary = summary_by_cycle.get(row.decision_cycle_id)
        terminal_at_cutoff = cutoff is None or row.closed_at is None or row.closed_at < cutoff
        analytic_counterfactuals.append(
            AnalyticsCounterfactual(
                rejection_gate=row.rejection_gate,
                status=row.status if terminal_at_cutoff else "open",
                hypothetical_pnl=row.hypothetical_pnl if terminal_at_cutoff else None,
                consensus_probability=(
                    summary.consensus_probability if summary is not None else None
                ),
                agent_probabilities=probabilities,
            )
        )

    sensitivities = tuple(
        ResearchSensitivityDatum(
            id=sensitivity.id,
            order_intent_id=order.id,
            proposal_id=proposal.id,
            decision_cycle_id=proposal.decision_cycle_id,
            symbol=order.symbol,
            scenario=sensitivity.scenario,
            calculated_at=sensitivity.calculated_at,
            outcome=dict(sensitivity.outcome_json),
        )
        for sensitivity, order, proposal in sensitivity_rows
    )
    analytics = build_research_analytics(
        initial_capital=experiment.initial_capital,
        snapshots=(
            AnalyticsSnapshot(
                snapshot_at=row.snapshot_at,
                equity=row.equity,
                drawdown=row.drawdown,
                realized_pnl=row.realized_pnl,
                unrealized_pnl=row.unrealized_pnl,
                fees=row.fees,
                funding=row.funding,
            )
            for row in snapshots
        ),
        fills=analytic_fills,
        bars=(
            AnalyticsBar(
                symbol=row.symbol,
                bar_close_at=row.bar_close_at,
                high=row.high,
                low=row.low,
                close=row.close,
            )
            for row in bars
        ),
        counterfactuals=analytic_counterfactuals,
        sensitivities=(
            AnalyticsSensitivity(
                scenario=row.scenario,
                execution_cost=_optional_decimal(row.outcome.get("execution_cost")) or Decimal("0"),
                marked_pnl=_optional_decimal(row.outcome.get("marked_pnl")) or Decimal("0"),
            )
            for row in sensitivities
        ),
    )
    return ResearchDataset(
        analytics=analytics,
        analytics_as_of=snapshots[-1].snapshot_at if snapshots else None,
        counterfactuals=tuple(counterfactuals),
        execution_sensitivities=sensitivities,
    )
