"""Authoritative SQLAlchemy projections imported by Alembic metadata discovery."""

from maais.db.models.accounts import (
    AccountSnapshotModel,
    ExitPlanModel,
    FundingEntryModel,
    PositionLotModel,
    PositionModel,
)
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    DecisionSummaryModel,
    GateEvaluationModel,
    MarketFrameModel,
    TradeProposalModel,
)
from maais.db.models.execution import (
    ExecutionSensitivityModel,
    FillModel,
    OrderEventModel,
    OrderIntentModel,
)
from maais.db.models.experiments import AgentVersionModel, ExperimentModel, StrategyVersionModel
from maais.db.models.ledger import DomainEventModel, EventStreamModel, OutboxEventModel

__all__ = [
    "AccountSnapshotModel",
    "AgentVersionModel",
    "AgentEvaluationModel",
    "DecisionCycleModel",
    "DecisionSummaryModel",
    "DomainEventModel",
    "EventStreamModel",
    "ExecutionSensitivityModel",
    "ExitPlanModel",
    "ExperimentModel",
    "FillModel",
    "FundingEntryModel",
    "GateEvaluationModel",
    "MarketFrameModel",
    "OutboxEventModel",
    "OrderEventModel",
    "OrderIntentModel",
    "PositionLotModel",
    "PositionModel",
    "StrategyVersionModel",
    "TradeProposalModel",
]
