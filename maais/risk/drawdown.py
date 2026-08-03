"""Drawdown controller — Rule 15 (BEHAVIOURS.md).

Thresholds:
  < 5%   → normal (multiplier 1.0)
  5–10%  → reduce 25% (multiplier 0.75)
  10–15% → reduce 50% (multiplier 0.50)
  ≥ 20%  → halt (multiplier 0.0)

Note: 15–20% range uses 0.50 multiplier (same as 10–15% tier).
"""

from maais.config.constants import (
    DRAWDOWN_HALT_THRESHOLD,
    DRAWDOWN_REDUCE_25_THRESHOLD,
    DRAWDOWN_REDUCE_50_THRESHOLD,
)
from maais.core.logging import get_logger
from maais.risk.schemas import DrawdownState

logger = get_logger(__name__)


class DrawdownController:
    """Tracks peak capital and computes position size multiplier.

    Call update() after each trade to track peak capital.
    Call get_state() to get the current multiplier.
    """

    def __init__(self, initial_capital: float) -> None:
        self._peak_capital = initial_capital
        self._current_capital = initial_capital

    def update(self, current_capital: float) -> None:
        """Update current capital. Advances peak if new high."""
        self._current_capital = current_capital
        if current_capital > self._peak_capital:
            self._peak_capital = current_capital

    def get_state(self) -> DrawdownState:
        """Return current drawdown state with multiplier."""
        if self._peak_capital <= 0:
            return DrawdownState(
                peak_capital=self._peak_capital,
                current_capital=self._current_capital,
                drawdown_pct=0.0,
                multiplier=1.0,
                is_halted=False,
            )

        drawdown_pct = (self._peak_capital - self._current_capital) / self._peak_capital
        multiplier, is_halted = _get_multiplier(drawdown_pct)

        if is_halted:
            logger.warning(
                "drawdown_halt_triggered",
                drawdown_pct=f"{drawdown_pct:.2%}",
                peak=self._peak_capital,
                current=self._current_capital,
            )
        elif multiplier < 1.0:
            logger.info(
                "drawdown_size_reduction",
                drawdown_pct=f"{drawdown_pct:.2%}",
                multiplier=multiplier,
            )

        return DrawdownState(
            peak_capital=self._peak_capital,
            current_capital=self._current_capital,
            drawdown_pct=drawdown_pct,
            multiplier=multiplier,
            is_halted=is_halted,
        )

    @property
    def peak_capital(self) -> float:
        return self._peak_capital

    @property
    def current_capital(self) -> float:
        return self._current_capital


def _get_multiplier(drawdown_pct: float) -> tuple[float, bool]:
    """Return (multiplier, is_halted) for a given drawdown level."""
    if drawdown_pct >= DRAWDOWN_HALT_THRESHOLD:  # >= 20%
        return 0.0, True
    if drawdown_pct >= DRAWDOWN_REDUCE_50_THRESHOLD:  # >= 10%
        return 0.50, False
    if drawdown_pct >= DRAWDOWN_REDUCE_25_THRESHOLD:  # >= 5%
        return 0.75, False
    return 1.0, False
