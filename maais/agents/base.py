"""Base agent class and AgentOutput — Rule 3 (BEHAVIOURS.md).

Every agent must produce exactly 4 outputs:
  1. directional_hypothesis  — "long" | "short" | "neutral"
  2. probability_estimate    — float ∈ [0.0, 1.0]  (P(hypothesis is correct))
  3. confidence_score        — float ∈ [0.0, 1.0]  (confidence in the estimate)
  4. risk_estimate           — float ∈ [0.0, 1.0]  (estimated trade risk)
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from maais.feature_pipeline.features import FeatureSet


@dataclass
class AgentOutput:
    """Rule 3: exactly 4 outputs per agent."""

    agent_name: str
    directional_hypothesis: str  # "long" | "short" | "neutral"
    probability_estimate: float  # P(hypothesis is correct), ∈ [0.5, 1.0] when directional
    confidence_score: float  # data quality / signal agreement, ∈ [0.0, 1.0]
    risk_estimate: float  # estimated risk for this trade, ∈ [0.0, 1.0]

    def __post_init__(self) -> None:
        assert self.directional_hypothesis in ("long", "short", "neutral"), (
            f"Invalid hypothesis: {self.directional_hypothesis}"
        )
        assert 0.0 <= self.probability_estimate <= 1.0, (
            f"probability_estimate out of range: {self.probability_estimate}"
        )
        assert 0.0 <= self.confidence_score <= 1.0, (
            f"confidence_score out of range: {self.confidence_score}"
        )
        assert 0.0 <= self.risk_estimate <= 1.0, f"risk_estimate out of range: {self.risk_estimate}"

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "directional_hypothesis": self.directional_hypothesis,
            "probability_estimate": self.probability_estimate,
            "confidence_score": self.confidence_score,
            "risk_estimate": self.risk_estimate,
        }


def _neutral(agent_name: str, risk: float = 0.5) -> AgentOutput:
    """Return a neutral output when features are insufficient."""
    return AgentOutput(
        agent_name=agent_name,
        directional_hypothesis="neutral",
        probability_estimate=0.5,
        confidence_score=0.0,
        risk_estimate=_clip(risk),
    )


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _votes_to_probability(vote_sum: float, max_votes: float) -> float:
    """Convert a sum of votes (range [-max_votes, +max_votes]) to probability ∈ [0.5, 0.95]."""
    if max_votes == 0:
        return 0.5
    ratio = vote_sum / max_votes  # → [-1, +1]
    tanh_val = math.tanh(abs(ratio) * 2.0)  # → [0, 1], steeper mapping
    return _clip(0.5 + tanh_val * 0.45)  # → [0.5, 0.95]


def _signal_to_output(
    agent_name: str,
    vote_sum: float,
    max_votes: float,
    confidence: float,
    risk: float,
) -> AgentOutput:
    """Convert a signed vote sum to a full AgentOutput.

    vote_sum > 0 → long, < 0 → short, near 0 → neutral.
    """
    threshold = max_votes * 0.15  # require at least 15% net agreement
    if abs(vote_sum) < threshold:
        hypothesis = "neutral"
        probability = 0.5
    elif vote_sum > 0:
        hypothesis = "long"
        probability = _votes_to_probability(vote_sum, max_votes)
    else:
        hypothesis = "short"
        probability = _votes_to_probability(abs(vote_sum), max_votes)

    return AgentOutput(
        agent_name=agent_name,
        directional_hypothesis=hypothesis,
        probability_estimate=probability,
        confidence_score=_clip(confidence),
        risk_estimate=_clip(risk),
    )


class BaseAgent(ABC):
    """Abstract base class for all 8 analytical agents."""

    name: str  # must match an AgentName constant
    compatible_regimes: tuple[str, ...] = ()  # regimes where this agent activates (Rule 13)

    @abstractmethod
    async def analyze(self, features: FeatureSet) -> AgentOutput:
        """Produce all 4 required outputs from the feature set."""

    def is_compatible_with_regime(self, regime: str | None) -> bool:
        """Rule 13: agent only runs in compatible regimes."""
        if not self.compatible_regimes:
            return True  # empty = compatible with all
        return regime in self.compatible_regimes
