"""Decision Engine — orchestrates the full decision pipeline.

Pipeline (per symbol, per tick):
  1. compute_consensus()    — weighted aggregation of agent outputs
  2. run_adversarial()      — minority challenge (Rule 5)
  3. estimate_costs()       — exchange fees + slippage
  4. compute_ev()           — EV = P(win)×gain − P(loss)×loss + carry (Rule 1)
  5. evaluate_gate()        — approve/reject (Rule 1 + Rule 5)
  6. Return DecisionResult

All weights start at 1.0 (equal). Batch 9 updates them via the registry.
"""

from datetime import datetime, timezone

from maais.agents.base import AgentOutput
from maais.core.logging import get_logger
from maais.decision.adversarial import run_adversarial
from maais.decision.consensus import compute_consensus
from maais.decision.cost_estimator import estimate_costs
from maais.decision.ev_engine import compute_ev
from maais.decision.gate import evaluate_gate
from maais.decision.schemas import DecisionResult
from maais.decision.weights import AgentWeightRegistry
from maais.feature_pipeline.features import FeatureSet

logger = get_logger(__name__)


class DecisionEngine:
    """Evaluates agent outputs and produces a trade decision."""

    def __init__(self, weight_registry: AgentWeightRegistry | None = None) -> None:
        self.weights = weight_registry or AgentWeightRegistry()

    def evaluate(
        self,
        features: FeatureSet,
        agent_outputs: list[AgentOutput],
    ) -> DecisionResult:
        """Run the full decision pipeline.

        Args:
            features: Current FeatureSet for the symbol.
            agent_outputs: Outputs from run_agents().

        Returns:
            DecisionResult — approved=True means trade gate passed.
        """
        # 1. Weighted consensus
        consensus = compute_consensus(agent_outputs, self.weights)

        # 2. Adversarial challenge
        adversarial = run_adversarial(agent_outputs, consensus.direction, self.weights)

        # 3. Cost estimation
        costs = estimate_costs(features)

        # 4. EV calculation
        ev = compute_ev(
            p_win=consensus.weighted_probability,
            features=features,
            costs=costs,
            direction=consensus.direction,
        )

        # 5. Gate
        approved, rejection_reason = evaluate_gate(ev, adversarial, consensus.direction)

        result = DecisionResult(
            symbol=features.symbol,
            timeframe=features.timeframe,
            timestamp=features.timestamp,
            approved=approved,
            direction=consensus.direction,
            consensus=consensus,
            adversarial=adversarial,
            costs=costs,
            ev=ev,
            rejection_reason=rejection_reason,
        )

        logger.info(
            "decision_evaluated",
            symbol=features.symbol,
            approved=approved,
            direction=consensus.direction,
            net_ev=f"{ev.net_ev:.6f}",
            agents=consensus.agents_counted,
            rejection=rejection_reason,
        )

        return result
