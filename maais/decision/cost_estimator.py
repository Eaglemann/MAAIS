"""Transaction cost estimator — exchange fees + slippage model.

Slippage model: f(order_size_usd, volatility, liquidity)
  slippage ≈ ATR_pct × volatility_multiplier + spread_impact

Taker fee: 0.04% (Binance USDT-Perp standard)
"""

from maais.decision.schemas import CostEstimate
from maais.feature_pipeline.features import FeatureSet

# Binance USDT-Perp taker fee (Rule 20: never underestimate costs)
TAKER_FEE_PCT = 0.0004  # 0.04%

# Slippage scaling factors
_VOL_SLIPPAGE_MULTIPLIER = 0.5  # fraction of ATR% paid as slippage
_SPREAD_IMPACT_MULTIPLIER = 0.3  # fraction of bid-ask spread as slippage


def estimate_costs(
    features: FeatureSet,
    order_size_usd: float = 0.0,
) -> CostEstimate:
    """Estimate round-trip transaction costs.

    Both entry and exit costs are included (2× fee).
    Slippage is estimated from volatility and book spread.
    Funding carry is the expected carry income/cost over a 1-period hold.

    Args:
        features: Current FeatureSet (ATR, spread, funding).
        order_size_usd: Size of the order in USD (used for future market-impact model).

    Returns:
        CostEstimate with all cost components.
    """
    # Round-trip exchange fees (entry + exit)
    exchange_fee_pct = TAKER_FEE_PCT * 2

    # Slippage: volatility component + spread component
    slippage_pct = 0.0

    if features.atr is not None and features.zscore_mean and features.zscore_mean > 0:
        atr_pct = features.atr / features.zscore_mean  # ATR as fraction of price
        slippage_pct += atr_pct * _VOL_SLIPPAGE_MULTIPLIER

    if features.bid_ask_spread is not None:
        slippage_pct += features.bid_ask_spread * _SPREAD_IMPACT_MULTIPLIER

    total_cost_pct = exchange_fee_pct + slippage_pct

    # Funding carry: annualized / 3 periods per day → per-period carry
    # Positive = income for being on the rewarded side
    funding_carry_pct = 0.0
    if features.annualized_funding is not None:
        # Per-period (8h) carry income (if we're on the right side, handled by caller)
        funding_carry_pct = abs(features.annualized_funding) / (365 * 3)

    return CostEstimate(
        exchange_fee_pct=exchange_fee_pct,
        slippage_pct=slippage_pct,
        total_cost_pct=total_cost_pct,
        funding_carry_pct=funding_carry_pct,
    )
