"""Risk Engine schemas — output dataclasses for Batch 6."""

from dataclasses import dataclass


@dataclass
class PositionSize:
    """Final position size after all risk controls."""

    symbol: str
    capital: float  # total account capital in USD

    # Intermediate sizes (before min())
    half_kelly_fraction: float  # raw Half-Kelly fraction of capital
    vol_adjusted_fraction: float  # volatility-adjusted fraction
    risk_cap_fraction: float  # hard cap (1–2% of capital)

    # Multipliers applied on top
    drawdown_multiplier: float  # 1.0 / 0.75 / 0.50 / 0.0 (Rule 15)
    correlation_multiplier: float  # 1.0 / 0.80 / 0.60 / 0.0 (Rule 12)

    # Final outputs
    base_fraction: float  # min(half_kelly, vol_adjusted, risk_cap)
    final_fraction: float  # base × drawdown × correlation
    final_usd: float  # final_fraction × capital
    approved: bool  # False if halted or correlation-blocked
    rejection_reason: str | None  # set when approved=False


@dataclass
class DrawdownState:
    """Current drawdown state for the portfolio."""

    peak_capital: float
    current_capital: float
    drawdown_pct: float  # (peak - current) / peak
    multiplier: float  # position size multiplier
    is_halted: bool  # True when drawdown >= 20%
