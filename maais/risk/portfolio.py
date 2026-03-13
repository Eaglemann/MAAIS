"""Portfolio risk limiter — Rule 11 (BEHAVIOURS.md).

Total simultaneous exposure capped at 10–15% of capital.
Default cap: 15% (MAX_PORTFOLIO_RISK from constants).

Each open position contributes its notional_usd / capital to total exposure.
A new position is rejected if adding it would exceed the cap.
"""

from maais.config.constants import PORTFOLIO_RISK_LIMIT_PCT
from maais.core.logging import get_logger

logger = get_logger(__name__)


class PortfolioRiskLimiter:
    """Tracks notional exposure of open positions and enforces portfolio cap.

    Usage:
        limiter.add_position(symbol, notional_usd)
        limiter.remove_position(symbol)
        ok, reason = limiter.can_add_position(symbol, proposed_usd, capital)
    """

    def __init__(self, cap_pct: float = PORTFOLIO_RISK_LIMIT_PCT) -> None:
        self._cap_pct = cap_pct
        self._positions: dict[str, float] = {}   # symbol → notional_usd

    def add_position(self, symbol: str, notional_usd: float) -> None:
        """Record an open position."""
        self._positions[symbol] = notional_usd

    def remove_position(self, symbol: str) -> None:
        """Remove a closed position."""
        self._positions.pop(symbol, None)

    def total_exposure_usd(self) -> float:
        return sum(self._positions.values())

    def total_exposure_pct(self, capital: float) -> float:
        if capital <= 0:
            return 0.0
        return self.total_exposure_usd() / capital

    def can_add_position(
        self,
        symbol: str,
        proposed_usd: float,
        capital: float,
    ) -> tuple[bool, str | None]:
        """Check if adding a new position would stay within the portfolio cap.

        If the symbol already has a position, the existing notional is replaced.
        Returns (allowed: bool, reason: str | None).
        """
        existing = self._positions.get(symbol, 0.0)
        current_total = self.total_exposure_usd() - existing
        new_total = current_total + proposed_usd
        new_pct = new_total / capital if capital > 0 else 0.0

        if new_pct > self._cap_pct:
            reason = (
                f"portfolio_cap_exceeded: "
                f"current={current_total/capital:.2%}, "
                f"proposed_addition={proposed_usd/capital:.2%}, "
                f"cap={self._cap_pct:.2%}"
            )
            logger.info("portfolio_cap_rejected", symbol=symbol, new_exposure_pct=f"{new_pct:.2%}", cap=f"{self._cap_pct:.2%}")
            return False, reason

        return True, None

    @property
    def cap_pct(self) -> float:
        return self._cap_pct

    def open_positions(self) -> dict[str, float]:
        return dict(self._positions)
