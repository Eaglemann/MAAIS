"""Decision Engine schemas — output dataclasses for Batch 5."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ConsensusResult:
    """Weighted aggregation of all agent outputs."""

    direction: str  # "long" | "short" | "neutral"
    weighted_probability: float  # Σ(w × p) / Σ(w) for winning direction
    weighted_confidence: float  # Σ(w × c) / Σ(w)
    long_weight_sum: float
    short_weight_sum: float
    neutral_weight_sum: float
    agents_counted: int
    agents_long: list[str]  # agent names voting long
    agents_short: list[str]  # agent names voting short
    agents_neutral: list[str]  # agent names voting neutral


@dataclass
class AdversarialSummary:
    """Record of the adversarial challenge step (Rule 5)."""

    majority_direction: str
    minority_direction: str
    dissenting_agents: list[str]  # names of agents on losing side
    dissenting_probability: float  # average probability from dissenters
    dissenting_confidence: float  # average confidence from dissenters
    challenge_accepted: bool  # True if minority case is strong enough to block
    # LLM reasoning placeholder — populated when Ollama is wired in (Batch 5 stub)
    llm_reasoning: str | None = None


@dataclass
class CostEstimate:
    """Estimated transaction costs for a potential trade."""

    exchange_fee_pct: float  # taker fee as fraction of trade value
    slippage_pct: float  # estimated slippage as fraction
    total_cost_pct: float  # exchange_fee_pct + slippage_pct
    funding_carry_pct: float  # annualized funding / periods_per_year (positive = income)


@dataclass
class EVResult:
    """Expected Value calculation (Rule 1)."""

    p_win: float
    p_loss: float
    expected_gain_pct: float  # ATR-based expected gain
    expected_loss_pct: float  # ATR-based expected loss
    gross_ev: float  # P(win)×gain − P(loss)×loss
    funding_carry: float  # carry income/cost over holding period
    net_ev: float  # gross_ev + funding_carry − total_cost
    is_positive: bool  # net_ev > 0 (Rule 1 gate)


@dataclass
class DecisionResult:
    """Full decision record produced by the Decision Engine."""

    symbol: str
    timeframe: str
    timestamp: datetime

    # Core decision
    approved: bool  # True = trade gate passed (EV > 0 after costs)
    direction: str  # "long" | "short" | "neutral"

    # Sub-results
    consensus: ConsensusResult
    adversarial: AdversarialSummary
    costs: CostEstimate
    ev: EVResult

    # Rejection reason if not approved
    rejection_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp.isoformat(),
            "approved": self.approved,
            "direction": self.direction,
            "weighted_probability": self.consensus.weighted_probability,
            "weighted_confidence": self.consensus.weighted_confidence,
            "net_ev": self.ev.net_ev,
            "total_cost_pct": self.costs.total_cost_pct,
            "agents_counted": self.consensus.agents_counted,
            "rejection_reason": self.rejection_reason,
        }
