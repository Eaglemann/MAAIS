"""Carry Yield Agent — funding rate and carry opportunities (Rule 7).

Funding rate logic for perpetual futures:
  long_heavy  (positive rate) → longs pay shorts → bearish bias (shorts being rewarded)
  short_heavy (negative rate) → shorts pay longs → bullish bias (longs being rewarded)

High annualized funding magnifies the signal (better carry opportunity).
"""

import math

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral
from maais.config.constants import AgentName
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.CARRY_YIELD
_HIGH_ANNUAL_FUNDING_THRESHOLD = 0.50  # 50% annualized = very high


class CarryYieldAgent(BaseAgent):
    name = _NAME
    compatible_regimes = ()  # funding rate analysis relevant in all regimes

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        if features.funding_bias is None or features.funding_rate is None:
            return _neutral(_NAME)

        rate = abs(features.funding_rate)
        annualised = (
            abs(features.annualized_funding) if features.annualized_funding else rate * 1095
        )

        # Direction: fade the crowded side (longs paying → short bias)
        if features.funding_bias == "long_heavy":
            hypothesis = "short"
        elif features.funding_bias == "short_heavy":
            hypothesis = "long"
        else:
            return _neutral(_NAME)

        # Signal strength scales with annualized funding
        strength = min(1.0, annualised / _HIGH_ANNUAL_FUNDING_THRESHOLD)
        probability = _clip(0.52 + math.tanh(strength * 2) * 0.33)
        confidence = _clip(strength)
        risk = _clip(features.rolling_std * 15.0) if features.rolling_std else 0.3

        return AgentOutput(
            agent_name=_NAME,
            directional_hypothesis=hypothesis,
            probability_estimate=probability,
            confidence_score=confidence,
            risk_estimate=risk,
        )
