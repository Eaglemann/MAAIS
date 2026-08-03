"""Half-Kelly position sizing and volatility normalization.

Half-Kelly formula:
  Full Kelly: f = (p*b - q) / b   where b = gain/loss ratio, q = 1 - p
  Half Kelly: f* = f / 2

Volatility normalization:
  Size is inversely proportional to ATR/price (more volatile = smaller position).
  vol_fraction = (target_risk_pct) / (atr_pct)
  where target_risk_pct is the acceptable risk per trade (e.g. 1.5%).
"""

from maais.config.constants import MAX_RISK_PER_TRADE_PCT

# Default target risk per trade for volatility normalization
_TARGET_RISK_PCT = MAX_RISK_PER_TRADE_PCT  # 2% max, use as target


def half_kelly_fraction(p_win: float, gain_pct: float, loss_pct: float) -> float:
    """Compute Half-Kelly fraction of capital to risk.

    Args:
        p_win: Probability of winning (from consensus).
        gain_pct: Expected gain as fraction of trade value (e.g. 0.01 = 1%).
        loss_pct: Expected loss as fraction of trade value (e.g. 0.01 = 1%).

    Returns:
        Half-Kelly fraction ∈ [0.0, 0.25] (capped to prevent extreme sizing).
    """
    if gain_pct <= 0 or loss_pct <= 0:
        return 0.0

    b = gain_pct / loss_pct  # reward-to-risk ratio
    q = 1.0 - p_win
    full_kelly = (p_win * b - q) / b

    if full_kelly <= 0:
        return 0.0

    half_kelly = full_kelly / 2.0
    return min(half_kelly, 0.25)  # hard cap at 25% of capital


def volatility_adjusted_fraction(
    atr: float | None,
    price: float | None,
    target_risk_pct: float = _TARGET_RISK_PCT,
) -> float:
    """Compute position fraction based on volatility normalization.

    Size = target_risk_pct / atr_pct
    Capped at 25% of capital to prevent oversizing in low-volatility regimes.

    Args:
        atr: Average True Range in price units.
        price: Current price (used as denominator for atr_pct).
        target_risk_pct: Acceptable loss per trade as fraction of capital.

    Returns:
        Fraction of capital ∈ (0.0, 0.25].
    """
    if atr is None or price is None or price <= 0 or atr <= 0:
        return target_risk_pct  # fallback: use target directly

    atr_pct = atr / price
    if atr_pct <= 0:
        return target_risk_pct

    fraction = target_risk_pct / atr_pct
    return min(fraction, 0.25)
