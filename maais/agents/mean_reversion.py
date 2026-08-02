"""Mean Reversion Agent — uses Z-score (Rule 6).

Triggers when |Z| > 3. Direction is opposite to the deviation:
  Z > +3  → short hypothesis (price is above mean, expect reversion downward)
  Z < −3  → long hypothesis  (price is below mean, expect reversion upward)

Probability scales with Z-score magnitude. This agent is most effective in
range-bound and low-volatility regimes.
"""

import math

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral
from maais.config.constants import ZSCORE_TRIGGER, AgentName, Regime
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.MEAN_REVERSION


class MeanReversionAgent(BaseAgent):
    name = _NAME
    compatible_regimes = (Regime.RANGE_BOUND, Regime.LOW_VOLATILITY)

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        z = features.zscore
        if z is None:
            return _neutral(_NAME)

        abs_z = abs(z)
        if abs_z <= ZSCORE_TRIGGER:
            return _neutral(_NAME, risk=_volatility_risk(features))

        # Probability: scales from 0.60 at |Z|=3 to 0.90 at |Z|≥6
        probability = _clip(0.50 + math.tanh((abs_z - ZSCORE_TRIGGER) * 0.5) * 0.40)
        confidence = _clip((abs_z - ZSCORE_TRIGGER) / 4.0)  # 0 at Z=3, 1.0 at Z=7
        risk = _volatility_risk(features)

        return AgentOutput(
            agent_name=_NAME,
            directional_hypothesis="short" if z > 0 else "long",
            probability_estimate=probability,
            confidence_score=confidence,
            risk_estimate=risk,
        )


def _volatility_risk(features: FeatureSet) -> float:
    if features.rolling_std is not None:
        return _clip(features.rolling_std * 20.0)  # scale small return-std to [0,1]
    if features.atr is not None and features.zscore_mean and features.zscore_mean > 0:
        return _clip(features.atr / features.zscore_mean)
    return 0.3
