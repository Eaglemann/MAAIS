"""Stop-Run Detection Agent — stop-hunt and liquidity sweep identification.

Logic: An extreme Z-score combined with elevated volatility suggests a
stop-hunt (price spikes to trigger stop-loss orders then reverses).
After a sweep, we expect a mean-reversion move OPPOSITE to the spike.
"""

import math

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral
from maais.config.constants import AgentName, Regime, ZSCORE_TRIGGER
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.STOP_RUN_DETECTION
_STOP_RUN_Z_THRESHOLD = ZSCORE_TRIGGER + 1.0    # slightly above mean reversion trigger


class StopRunDetectionAgent(BaseAgent):
    name = _NAME
    compatible_regimes = (Regime.HIGH_VOLATILITY, Regime.TRENDING)

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        z = features.zscore
        vol = features.rolling_std
        atr = features.atr

        if z is None:
            return _neutral(_NAME)

        abs_z = abs(z)
        if abs_z < _STOP_RUN_Z_THRESHOLD:
            return _neutral(_NAME, risk=_vol_risk(vol, atr))

        # Stop-run detected: trade OPPOSITE the spike direction
        hypothesis = "short" if z > 0 else "long"

        # Confidence: requires both high Z AND elevated volatility
        vol_elevated = vol is not None and vol > 0.002   # ~0.2% std on returns
        base_prob = 0.55 + math.tanh((abs_z - _STOP_RUN_Z_THRESHOLD) * 0.8) * 0.30
        probability = _clip(base_prob * (1.2 if vol_elevated else 0.9))
        confidence = _clip(0.4 + (0.3 if vol_elevated else 0.0))
        risk = _vol_risk(vol, atr)

        return AgentOutput(
            agent_name=_NAME,
            directional_hypothesis=hypothesis,
            probability_estimate=probability,
            confidence_score=confidence,
            risk_estimate=risk,
        )


def _vol_risk(vol: float | None, atr: float | None) -> float:
    if vol is not None:
        return _clip(vol * 25.0)
    return 0.4
