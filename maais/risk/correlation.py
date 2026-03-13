"""Correlation controller — Rule 12 (BEHAVIOURS.md).

Rolling 60-period correlation between asset close returns.
Thresholds:
  < 0.30  → full allocation (multiplier 1.0)
  0.30–0.60 → reduce 20% (multiplier 0.80)
  0.60–0.70 → reduce 40% (multiplier 0.60)
  ≥ 0.70  → cluster treatment — block position (multiplier 0.0)

Rolling window: 60 periods (confirmed in ORCHESTRATOR.md Q21).
"""

import statistics

from maais.config.constants import (
    CORRELATION_FULL_THRESHOLD,
    CORRELATION_REDUCE_20_THRESHOLD,
    CORRELATION_REDUCE_40_THRESHOLD,
    CORRELATION_ROLLING_WINDOW,
)
from maais.core.logging import get_logger

logger = get_logger(__name__)


class CorrelationController:
    """Tracks return history per symbol and computes pairwise correlation multipliers.

    Usage:
        controller.add_return(symbol, close_return)   # call each bar
        multiplier = controller.get_multiplier(symbol_a, symbol_b)
    """

    def __init__(self, window: int = CORRELATION_ROLLING_WINDOW) -> None:
        self._window = window
        self._returns: dict[str, list[float]] = {}

    def add_return(self, symbol: str, close_return: float) -> None:
        """Record a new close-to-close return for a symbol."""
        if symbol not in self._returns:
            self._returns[symbol] = []
        self._returns[symbol].append(close_return)
        # Keep only the rolling window
        if len(self._returns[symbol]) > self._window:
            self._returns[symbol].pop(0)

    def get_correlation(self, symbol_a: str, symbol_b: str) -> float | None:
        """Compute Pearson correlation between two symbols over the rolling window.

        Returns None if insufficient data (< 2 overlapping observations).
        """
        returns_a = self._returns.get(symbol_a, [])
        returns_b = self._returns.get(symbol_b, [])

        n = min(len(returns_a), len(returns_b))
        if n < 2:
            return None

        # Use the most recent n observations from each
        a = returns_a[-n:]
        b = returns_b[-n:]

        try:
            return statistics.correlation(a, b)
        except statistics.StatisticsError:
            return None

    def get_multiplier(self, symbol_a: str, symbol_b: str) -> float:
        """Return position size multiplier based on correlation between two symbols.

        Returns 1.0 if correlation cannot be computed (insufficient data — conservative default).
        """
        corr = self.get_correlation(symbol_a, symbol_b)
        if corr is None:
            return 1.0   # unknown correlation → full allocation (no data = no restriction)

        abs_corr = abs(corr)
        multiplier = _correlation_multiplier(abs_corr)

        if multiplier < 1.0:
            logger.info(
                "correlation_size_reduction",
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                correlation=f"{abs_corr:.3f}",
                multiplier=multiplier,
            )

        return multiplier

    def symbols_tracked(self) -> list[str]:
        return list(self._returns.keys())


def _correlation_multiplier(abs_corr: float) -> float:
    """Map absolute correlation value to position size multiplier (Rule 12)."""
    if abs_corr >= CORRELATION_REDUCE_40_THRESHOLD:  # >= 0.70
        return 0.0   # cluster treatment — block
    if abs_corr >= CORRELATION_REDUCE_20_THRESHOLD:  # >= 0.60
        return 0.60  # reduce 40%
    if abs_corr >= CORRELATION_FULL_THRESHOLD:        # >= 0.30
        return 0.80  # reduce 20%
    return 1.0
