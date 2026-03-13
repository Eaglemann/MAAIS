"""Drawdown monitor — wraps DrawdownController, fires alerts at each tier (Rule 15).

Alert tiers:
  drawdown >= 5%  → WARNING
  drawdown >= 10% → WARNING (stronger)
  drawdown >= 15% → CRITICAL
  drawdown >= 20% → CRITICAL + triggers kill-switch
"""

from __future__ import annotations

from maais.config.constants import (
    DRAWDOWN_HALT_THRESHOLD,
    DRAWDOWN_REDUCE_25_THRESHOLD,
    DRAWDOWN_REDUCE_50_THRESHOLD,
)
from maais.core.logging import get_logger
from maais.monitoring.alerting import AlertDispatcher
from maais.monitoring.kill_switch import KillSwitch
from maais.risk.drawdown import DrawdownController

logger = get_logger(__name__)

_DRAWDOWN_CRITICAL_THRESHOLD = 0.15   # 15% → CRITICAL alert


class DrawdownMonitor:
    """Monitors drawdown levels and fires alerts / kill-switch as appropriate."""

    def __init__(
        self,
        drawdown_controller: DrawdownController,
        alert_dispatcher: AlertDispatcher,
        kill_switch: KillSwitch,
    ) -> None:
        self._dd = drawdown_controller
        self._alerts = alert_dispatcher
        self._kill_switch = kill_switch
        self._last_alerted_tier: float = 0.0   # avoid repeated alerts for the same tier

    async def check(self) -> None:
        """Read current drawdown state and fire appropriate alerts."""
        state = self._dd.get_state()
        pct = state.drawdown_pct

        if state.is_halted and self._last_alerted_tier < DRAWDOWN_HALT_THRESHOLD:
            self._last_alerted_tier = DRAWDOWN_HALT_THRESHOLD
            await self._alerts.send_critical(
                component="drawdown_monitor",
                title="TRADING HALT — Max Drawdown Exceeded",
                message=f"Drawdown {pct:.2%} >= {DRAWDOWN_HALT_THRESHOLD:.0%} threshold. All trading suspended.",
                drawdown_pct=f"{pct:.4f}",
                peak=state.peak_capital,
                current=state.current_capital,
            )
            if not self._kill_switch.is_halted():
                self._kill_switch.halt(f"drawdown_halt: {pct:.2%}")
            return

        if pct >= _DRAWDOWN_CRITICAL_THRESHOLD and self._last_alerted_tier < _DRAWDOWN_CRITICAL_THRESHOLD:
            self._last_alerted_tier = _DRAWDOWN_CRITICAL_THRESHOLD
            await self._alerts.send_critical(
                component="drawdown_monitor",
                title="Critical Drawdown — 15%+",
                message=f"Drawdown {pct:.2%}. Position sizes reduced 50%.",
                drawdown_pct=f"{pct:.4f}",
            )
            return

        if pct >= DRAWDOWN_REDUCE_50_THRESHOLD and self._last_alerted_tier < DRAWDOWN_REDUCE_50_THRESHOLD:
            self._last_alerted_tier = DRAWDOWN_REDUCE_50_THRESHOLD
            await self._alerts.send_warning(
                component="drawdown_monitor",
                title="Drawdown Warning — 10%+",
                message=f"Drawdown {pct:.2%}. Position sizes reduced 50%.",
                drawdown_pct=f"{pct:.4f}",
            )
            return

        if pct >= DRAWDOWN_REDUCE_25_THRESHOLD and self._last_alerted_tier < DRAWDOWN_REDUCE_25_THRESHOLD:
            self._last_alerted_tier = DRAWDOWN_REDUCE_25_THRESHOLD
            await self._alerts.send_warning(
                component="drawdown_monitor",
                title="Drawdown Warning — 5%+",
                message=f"Drawdown {pct:.2%}. Position sizes reduced 25%.",
                drawdown_pct=f"{pct:.4f}",
            )

        # Reset tier tracker when drawdown recovers
        if pct < DRAWDOWN_REDUCE_25_THRESHOLD:
            self._last_alerted_tier = 0.0
