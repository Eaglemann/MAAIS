"""FeatureSet — the single output unit of the Feature Engineering Pipeline.

One FeatureSet per (symbol, timeframe, timestamp).
Consumed by all 8 analytical agents (Batch 4) and the regime gate (Rule 13).
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class FeatureSet:
    symbol: str
    timeframe: str
    timestamp: datetime

    # ── Z-score (Rule 6) ─────────────────────────────────────────────────────
    zscore: float | None = None  # Z = (Price − Mean) / Std; |Z| > 3 triggers
    zscore_mean: float | None = None
    zscore_std: float | None = None

    # ── Momentum ──────────────────────────────────────────────────────────────
    ema_fast: float | None = None  # fast EMA (default: 9 periods)
    ema_slow: float | None = None  # slow EMA (default: 21 periods)
    ema_signal: float | None = None  # ema_fast − ema_slow (positive = bullish bias)
    roc_short: float | None = None  # rate-of-change, short window
    roc_long: float | None = None  # rate-of-change, long window

    # ── Volatility ────────────────────────────────────────────────────────────
    atr: float | None = None  # Average True Range (default: 14 periods)
    rolling_std: float | None = None  # rolling std of close-to-close returns

    # ── Order Book ────────────────────────────────────────────────────────────
    bid_ask_spread: float | None = None  # (best_ask − best_bid) / mid_price
    book_imbalance: float | None = None  # (bid_vol − ask_vol) / total_vol ∈ [−1, +1]

    # ── Funding Rate ──────────────────────────────────────────────────────────
    funding_rate: float | None = None
    annualized_funding: float | None = None
    funding_bias: str | None = None  # "long_heavy" | "short_heavy" | "neutral"

    # ── Regime (Rule 13) ─────────────────────────────────────────────────────
    regime: str | None = None  # "trending"|"range_bound"|"high_volatility"|"low_volatility"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp.isoformat(),
            "zscore": self.zscore,
            "zscore_mean": self.zscore_mean,
            "zscore_std": self.zscore_std,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "ema_signal": self.ema_signal,
            "roc_short": self.roc_short,
            "roc_long": self.roc_long,
            "atr": self.atr,
            "rolling_std": self.rolling_std,
            "bid_ask_spread": self.bid_ask_spread,
            "book_imbalance": self.book_imbalance,
            "funding_rate": self.funding_rate,
            "annualized_funding": self.annualized_funding,
            "funding_bias": self.funding_bias,
            "regime": self.regime,
        }
