"""Market regime classifier — Rule 13 (BEHAVIOURS.md).

Classifies into exactly 4 regimes:
  trending        — sustained directional movement (strong EMA divergence)
  range_bound     — price oscillating around mean (weak directional signal)
  high_volatility — elevated rolling std vs historical baseline
  low_volatility  — compressed rolling std vs historical baseline

Priority order: volatility check first (extreme conditions override trend signal).
"""

import statistics

from maais.config.constants import (
    HIGH_VOL_MULTIPLIER,
    LOW_VOL_MULTIPLIER,
    REGIME_LOOKBACK,
    REGIME_TREND_THRESHOLD,
    Regime,
)


def classify_regime(
    closes: list[float],
    rolling_std: float | None,
    ema_fast: float | None,
    ema_slow: float | None,
    lookback: int = REGIME_LOOKBACK,
) -> str:
    """Classify current market regime from price series and computed indicators.

    Returns one of: Regime.TRENDING, RANGE_BOUND, HIGH_VOLATILITY, LOW_VOLATILITY.
    Falls back to RANGE_BOUND if insufficient data.
    """
    # Need enough data for a baseline std
    if len(closes) < lookback or rolling_std is None:
        return Regime.RANGE_BOUND

    # Compute baseline volatility over the full lookback window
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes[-lookback:]))]
    if len(returns) < 2:
        return Regime.RANGE_BOUND

    try:
        baseline_std = statistics.stdev(returns)
    except statistics.StatisticsError:
        return Regime.RANGE_BOUND

    if baseline_std == 0:
        return Regime.LOW_VOLATILITY

    # 1. Volatility regimes (highest priority — override trend signal)
    vol_ratio = rolling_std / baseline_std
    if vol_ratio > HIGH_VOL_MULTIPLIER:
        return Regime.HIGH_VOLATILITY
    if vol_ratio < LOW_VOL_MULTIPLIER:
        return Regime.LOW_VOLATILITY

    # 2. Trend vs range (EMA divergence)
    if ema_fast is not None and ema_slow is not None and ema_slow != 0:
        divergence = abs(ema_fast - ema_slow) / ema_slow
        if divergence > REGIME_TREND_THRESHOLD:
            return Regime.TRENDING

    return Regime.RANGE_BOUND
