"""Macro Sentiment Agent — macroeconomic and sentiment signals.

In the current implementation (Phase 1), macro sentiment is proxied
from available technical features (regime + volatility + Z-score).
In a future batch, this agent will be enhanced with:
  - News article sentiment (stored as JSONB in PostgreSQL, Batch 1)
  - The Ollama LLM layer (wired at Decision Engine level, Batch 5)

Current logic:
  - High volatility regime → risk-off sentiment → short/neutral bias
  - Z-score extreme → sentiment divergence → mean-reversion lean
  - Low volatility → complacency → slight contrarian lean
"""

import math

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral, _signal_to_output
from maais.config.constants import AgentName, Regime, ZSCORE_TRIGGER
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.MACRO_SENTIMENT


class MacroSentimentAgent(BaseAgent):
    name = _NAME
    compatible_regimes = ()   # macro context is relevant in all regimes

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        votes, total = 0.0, 0.0
        regime = features.regime

        # Regime-based sentiment
        if regime == Regime.HIGH_VOLATILITY:
            votes -= 0.5   # risk-off → slight short bias
            total += 0.5
        elif regime == Regime.LOW_VOLATILITY:
            # Low vol = complacency; slight contrarian → no strong view
            total += 0.5
        elif regime == Regime.TRENDING:
            # Trend following = mild alignment with momentum
            if features.ema_signal is not None:
                votes += 0.5 if features.ema_signal > 0 else -0.5
                total += 0.5

        # Extreme Z-score = sentiment divergence from fair value
        if features.zscore is not None and abs(features.zscore) > ZSCORE_TRIGGER:
            # Lean against the extreme
            votes += -0.3 if features.zscore > 0 else 0.3
            total += 0.3

        if total == 0:
            return _neutral(_NAME)

        confidence = 0.3   # low confidence — pure proxy logic until real news feed
        risk = _clip(features.rolling_std * 15.0) if features.rolling_std else 0.4

        return _signal_to_output(_NAME, votes, total, confidence, risk)
