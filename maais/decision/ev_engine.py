"""Expected Value engine — Rule 1 (BEHAVIOURS.md).

EV = P(win) × Gain − P(loss) × Loss + funding_carry

Gain and Loss are estimated from ATR (Average True Range):
  - Expected gain: consensus_probability × ATR (risk/reward 1:1 at minimum)
  - Expected loss: (1 - consensus_probability) × ATR

The EV must be positive after all costs for the trade gate to pass.
"""

from maais.decision.schemas import CostEstimate, EVResult
from maais.feature_pipeline.features import FeatureSet

# Minimum reward-to-risk ratio required
_MIN_GAIN_LOSS_RATIO = 1.0


def compute_ev(
    p_win: float,
    features: FeatureSet,
    costs: CostEstimate,
    direction: str,
) -> EVResult:
    """Compute expected value for a potential trade.

    Args:
        p_win: Weighted probability from consensus (P(hypothesis is correct)).
        features: Current FeatureSet (ATR, funding).
        costs: Estimated transaction costs.
        direction: "long" | "short" — determines funding carry sign.

    Returns:
        EVResult with gross EV, carry, net EV, and gate verdict.
    """
    p_loss = 1.0 - p_win

    # Gain/loss estimate from ATR as fraction of price
    if features.atr is not None and features.zscore_mean and features.zscore_mean > 0:
        atr_pct = features.atr / features.zscore_mean
    else:
        # Fallback: assume 0.5% move (conservative)
        atr_pct = 0.005

    expected_gain_pct = atr_pct  # target: 1× ATR
    expected_loss_pct = atr_pct  # stop: 1× ATR (symmetric default)

    gross_ev = p_win * expected_gain_pct - p_loss * expected_loss_pct

    # Funding carry: positive if we're on the rewarded side
    # long_heavy funding → shorts earn → long = cost, short = income
    funding_carry = 0.0
    if features.funding_bias is not None and features.annualized_funding is not None:
        per_period = costs.funding_carry_pct
        if features.funding_bias == "long_heavy":
            # longs pay, shorts receive
            funding_carry = per_period if direction == "short" else -per_period
        elif features.funding_bias == "short_heavy":
            # shorts pay, longs receive
            funding_carry = per_period if direction == "long" else -per_period

    net_ev = gross_ev + funding_carry - costs.total_cost_pct

    return EVResult(
        p_win=p_win,
        p_loss=p_loss,
        expected_gain_pct=expected_gain_pct,
        expected_loss_pct=expected_loss_pct,
        gross_ev=gross_ev,
        funding_carry=funding_carry,
        net_ev=net_ev,
        is_positive=net_ev > 0,
    )
