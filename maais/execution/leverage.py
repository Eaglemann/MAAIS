"""Leverage enforcer — Rule 20 (BEHAVIOURS.md).

System-level leverage cap: 5× (confirmed in ORCHESTRATOR.md Q25).
Before any order, the enforcer:
  1. Checks the requested leverage
  2. Rejects if > MAX_LEVERAGE
  3. Calls Binance to set leverage if not already at the correct level
"""

from maais.config.constants import MAX_LEVERAGE
from maais.core.logging import get_logger

logger = get_logger(__name__)


class LeverageEnforcer:
    """Enforces the system-wide leverage cap on all positions."""

    def __init__(self, max_leverage: int = MAX_LEVERAGE) -> None:
        self._max_leverage = max_leverage
        # Cache of symbol → currently set leverage (avoids redundant API calls)
        self._set_leverage: dict[str, int] = {}

    def validate(self, symbol: str, requested_leverage: int) -> tuple[bool, str | None]:
        """Check if the requested leverage is within the system cap.

        Returns (allowed, reason). Reason is None when allowed.
        """
        if requested_leverage > self._max_leverage:
            reason = (
                f"leverage_cap_exceeded: requested={requested_leverage}x, "
                f"max={self._max_leverage}x (Rule 20)"
            )
            logger.warning(
                "leverage_rejected",
                symbol=symbol,
                requested=requested_leverage,
                cap=self._max_leverage,
            )
            return False, reason
        return True, None

    def needs_update(self, symbol: str, target_leverage: int) -> bool:
        """Return True if Binance needs to be called to set leverage."""
        return self._set_leverage.get(symbol) != target_leverage

    def record_set(self, symbol: str, leverage: int) -> None:
        """Record that leverage has been confirmed set on Binance."""
        self._set_leverage[symbol] = leverage
        logger.info("leverage_set", symbol=symbol, leverage=leverage)

    @property
    def max_leverage(self) -> int:
        return self._max_leverage
