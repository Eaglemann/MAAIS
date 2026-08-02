"""Technical Structure Agent — price pattern structure relative to moving averages."""

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral, _signal_to_output
from maais.config.constants import AgentName, Regime
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.TECHNICAL_STRUCTURE


class TechnicalStructureAgent(BaseAgent):
    name = _NAME
    compatible_regimes = (Regime.TRENDING, Regime.RANGE_BOUND)

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        votes, total = 0.0, 0.0

        # Price vs EMAs: use zscore_mean as proxy for current price level
        if features.ema_fast is not None and features.ema_slow is not None:
            # Both EMAs trending same direction
            if features.ema_fast > features.ema_slow:
                votes += 1.5  # bullish structure
            else:
                votes -= 1.5
            total += 1.5

        # EMA signal strength
        if features.ema_signal is not None and features.ema_slow and features.ema_slow != 0:
            strength = min(1.0, abs(features.ema_signal / features.ema_slow) * 50)
            votes += (1.0 if features.ema_signal > 0 else -1.0) * strength
            total += 1.0

        # Z-score as mean reversion risk modifier (extreme = risky for structure trades)
        z_risk = 0.3
        if features.zscore is not None:
            z_risk = _clip(abs(features.zscore) / 5.0)  # at Z=5 → risk=1.0

        if total == 0:
            return _neutral(_NAME, risk=z_risk)

        confidence = _clip(0.7 if (features.ema_fast and features.ema_slow) else 0.3)
        return _signal_to_output(_NAME, votes, total, confidence, z_risk)
