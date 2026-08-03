"""Trade gate — Rule 1 (EV > 0) and alpha validator — Rule 2.

The gate is the final check before a trade is approved:
  1. EV must be positive after all costs (Rule 1)
  2. Adversarial challenge must not be accepted (Rule 5)
  3. Direction must be non-neutral

Alpha validator (Rule 2): checks that the expected strategy return
exceeds the estimated market return (passive hold). Stored with the
DecisionResult for post-trade verification by Batch 9.
"""

from maais.decision.schemas import AdversarialSummary, EVResult


def evaluate_gate(
    ev: EVResult,
    adversarial: AdversarialSummary,
    direction: str,
) -> tuple[bool, str | None]:
    """Determine whether the trade gate passes.

    Returns:
        (approved: bool, rejection_reason: str | None)
    """
    if direction == "neutral":
        return False, "direction_is_neutral"

    if not ev.is_positive:
        return False, f"negative_ev: net_ev={ev.net_ev:.6f}"

    if adversarial.challenge_accepted:
        return False, (
            f"adversarial_challenge_accepted: "
            f"dissenters={adversarial.dissenting_agents}, "
            f"dissenter_confidence={adversarial.dissenting_confidence:.3f}"
        )

    return True, None


def alpha_valid(ev: EVResult, market_return_pct: float = 0.0) -> bool:
    """Rule 2: strategy expected return > market return after costs.

    market_return_pct: passive market return for the same period (default 0).
    """
    return ev.net_ev > market_return_pct
