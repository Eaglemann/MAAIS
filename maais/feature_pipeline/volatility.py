"""Volatility features: ATR and rolling standard deviation.

ATR period: 14 (ATR_PERIOD in config/constants.py).
"""

import statistics

from maais.config.constants import ATR_PERIOD
from maais.market_data.schemas import KlineData


def compute_true_range(candle: KlineData, prev_close: float) -> float:
    high = float(candle.high)
    low = float(candle.low)
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_atr(candles: list[KlineData], period: int = ATR_PERIOD) -> float | None:
    """Average True Range over `period` candles. Requires at least period+1 candles."""
    if len(candles) < period + 1:
        return None
    trs = [
        compute_true_range(candles[i], float(candles[i - 1].close)) for i in range(1, len(candles))
    ]
    return sum(trs[-period:]) / period


def compute_rolling_std(prices: list[float], period: int = ATR_PERIOD) -> float | None:
    """Rolling standard deviation of close-to-close returns (not raw prices).
    Returns None if insufficient data.
    """
    if len(prices) < period + 1:
        return None
    returns = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    window = returns[-period:]
    if len(window) < 2:
        return None
    return statistics.stdev(window)


def compute_volatility_features(candles: list[KlineData]) -> dict[str, float | None]:
    closes = [float(c.close) for c in candles]
    return {
        "atr": compute_atr(candles),
        "rolling_std": compute_rolling_std(closes),
    }
