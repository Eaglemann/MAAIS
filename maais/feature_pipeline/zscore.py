"""Z-score calculator — Rule 6 (BEHAVIOURS.md).

Z = (Price − Mean) / Standard Deviation
Trigger: |Z| > 3 signals a potential mean reversion opportunity.
Lookback default: 20 periods (ZSCORE_LOOKBACK in config/constants.py).
"""

import statistics

from maais.config.constants import ZSCORE_LOOKBACK, ZSCORE_TRIGGER


def compute_zscore(
    prices: list[float],
    lookback: int = ZSCORE_LOOKBACK,
) -> tuple[float | None, float | None, float | None]:
    """Compute Z-score for the most recent price.
    Returns (zscore, mean, std) — all None if insufficient data.
    """
    if len(prices) < lookback:
        return None, None, None
    window = prices[-lookback:]
    mean = statistics.mean(window)
    std = statistics.stdev(window)
    if std == 0:
        return 0.0, mean, 0.0
    return (prices[-1] - mean) / std, mean, std


def is_mean_reversion_signal(zscore: float | None) -> bool:
    """Rule 6: returns True when |Z| > 3."""
    if zscore is None:
        return False
    return abs(zscore) > ZSCORE_TRIGGER
