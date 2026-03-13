"""Momentum Agent — price trend strength and direction."""

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral, _signal_to_output
from maais.config.constants import AgentName, Regime
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.MOMENTUM


class MomentumAgent(BaseAgent):
    name = _NAME
    compatible_regimes = (Regime.TRENDING,)

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        votes, total = 0.0, 0.0

        # EMA crossover signal
        if features.ema_signal is not None and features.ema_slow and features.ema_slow != 0:
            normalised = features.ema_signal / features.ema_slow
            weight = 2.0
            votes += (1.0 if normalised > 0 else -1.0) * weight * min(1.0, abs(normalised) * 100)
            total += weight

        # Short-term momentum
        if features.roc_short is not None:
            votes += (1.0 if features.roc_short > 0 else -1.0) * min(1.0, abs(features.roc_short) * 20)
            total += 1.0

        # Long-term momentum (half weight)
        if features.roc_long is not None:
            votes += (1.0 if features.roc_long > 0 else -1.0) * 0.5 * min(1.0, abs(features.roc_long) * 10)
            total += 0.5

        if total == 0:
            return _neutral(_NAME)

        signals_present = sum(1 for x in [features.ema_signal, features.roc_short, features.roc_long] if x is not None)
        confidence = _clip(signals_present / 3.0)
        risk = _clip(features.rolling_std * 15.0) if features.rolling_std else 0.35

        return _signal_to_output(_NAME, votes, total, confidence, risk)
