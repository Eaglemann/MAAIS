"""Liquidity Agent — institutional liquidity patterns from order book."""

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral, _signal_to_output
from maais.config.constants import AgentName
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.LIQUIDITY


class LiquidityAgent(BaseAgent):
    name = _NAME
    compatible_regimes = ()  # compatible with all regimes

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        if features.book_imbalance is None:
            return _neutral(_NAME)

        imbalance = features.book_imbalance  # ∈ [-1, +1]
        votes = imbalance * 2.0  # scale to [-2, +2]
        total = 2.0

        # Wide spread = uncertainty = lower confidence
        spread_risk = 0.3
        confidence = 0.8
        if features.bid_ask_spread is not None:
            spread_risk = _clip(features.bid_ask_spread * 100)
            confidence = _clip(1.0 - spread_risk * 0.5)

        return _signal_to_output(_NAME, votes, total, confidence, spread_risk)
