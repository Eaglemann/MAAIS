"""Momentum indicators: EMA and Rate-of-Change.

Moving average type: EMA (chosen over SMA for responsiveness to recent price action).
Constants: EMA_FAST_PERIOD=9, EMA_SLOW_PERIOD=21, MOMENTUM_ROC_SHORT=5, MOMENTUM_ROC_LONG=20
"""

from maais.config.constants import EMA_FAST_PERIOD, EMA_SLOW_PERIOD, MOMENTUM_ROC_SHORT, MOMENTUM_ROC_LONG


def compute_ema(prices: list[float], period: int) -> float | None:
    """Exponential Moving Average. alpha = 2 / (period + 1).
    Returns None if len(prices) < period.
    Seeds the EMA with the mean of the first `period` values (proper initialisation).
    """
    if len(prices) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period  # seed with SMA
    for price in prices[period:]:
        ema = alpha * price + (1 - alpha) * ema
    return ema


def compute_roc(prices: list[float], period: int) -> float | None:
    """Rate of Change: (close[now] - close[n periods ago]) / close[n periods ago].
    Returns None if insufficient data.
    """
    if len(prices) < period + 1:
        return None
    base = prices[-(period + 1)]
    if base == 0:
        return None
    return (prices[-1] - base) / base


def compute_momentum_features(prices: list[float]) -> dict[str, float | None]:
    """Compute all momentum features from a price series."""
    ema_fast = compute_ema(prices, EMA_FAST_PERIOD)
    ema_slow = compute_ema(prices, EMA_SLOW_PERIOD)
    ema_signal = (ema_fast - ema_slow) if (ema_fast is not None and ema_slow is not None) else None
    return {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_signal": ema_signal,
        "roc_short": compute_roc(prices, MOMENTUM_ROC_SHORT),
        "roc_long": compute_roc(prices, MOMENTUM_ROC_LONG),
    }
