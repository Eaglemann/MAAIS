"""Authoritative SQLAlchemy projections imported by Alembic metadata discovery."""

from maais.db.models.accounts import (
    AccountSnapshotModel,
    ExitPlanModel,
    FundingEntryModel,
    PositionLotModel,
    PositionModel,
)
from maais.db.models.agents import AgentWeightModel
from maais.db.models.counterfactuals import CounterfactualModel
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
from maais.db.models.operations import (
    DataQualityEvaluationModel,
    IncidentModel,
    MarketCursorModel,
    MarketRecoveryRunModel,
    WorkerCheckpointModel,
)

__all__ = [
    "AccountSnapshotModel",
    "AgentVersionModel",
    "AgentWeightModel",
    "AgentEvaluationModel",
    "CounterfactualModel",
    "DecisionCycleModel",
    "DecisionSummaryModel",
    "DataQualityEvaluationModel",
    "DomainEventModel",
    "EventStreamModel",
    "ExecutionSensitivityModel",
    "ExitPlanModel",
    "ExperimentModel",
    "FillModel",
    "FundingEntryModel",
    "GateEvaluationModel",
    "IncidentModel",
    "MarketFrameModel",
    "MarketCursorModel",
    "MarketRecoveryRunModel",
    "OutboxEventModel",
    "OrderEventModel",
    "OrderIntentModel",
    "PositionLotModel",
    "PositionModel",
    "StrategyVersionModel",
    "TradeProposalModel",
    "WorkerCheckpointModel",
]
