"""Funding rate features for the Carry Yield Agent.

Annualized funding = rate × 3 × 365 (funding paid 3× per day on Binance Futures).
"""

from maais.config.constants import FUNDING_ANNUAL_MULTIPLIER, FUNDING_BIAS_THRESHOLD
from maais.market_data.schemas import FundingRateData


def compute_funding_features(funding: FundingRateData | None) -> dict[str, float | str | None]:
    if funding is None:
        return {"funding_rate": None, "annualized_funding": None, "funding_bias": None}

    rate = float(funding.funding_rate)
    annualized = rate * FUNDING_ANNUAL_MULTIPLIER

    if rate > FUNDING_BIAS_THRESHOLD:
        bias = "long_heavy"   # longs are paying; bearish pressure
    elif rate < -FUNDING_BIAS_THRESHOLD:
        bias = "short_heavy"  # shorts are paying; bullish pressure
    else:
        bias = "neutral"

    return {
        "funding_rate": rate,
        "annualized_funding": annualized,
        "funding_bias": bias,
    }
