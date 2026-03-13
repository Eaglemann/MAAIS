"""Monitoring Engine — orchestrates all monitoring checks.

Exposes is_trading_allowed(features, exposure_pct) which must return True
before any trade is approved. Blocks if:
  - KillSwitch is halted (Rule 17)
  - BlackSwanGuard triggers (Rule 18)

Also runs drawdown check on each call (fires alerts as needed).

Rule 20: override prevention — is_trading_allowed() cannot be bypassed.
"""

from __future__ import annotations

from maais.core.logging import get_logger
from maais.feature_pipeline.features import FeatureSet
from maais.monitoring.alerting import AlertDispatcher
from maais.monitoring.black_swan import BlackSwanGuard
from maais.monitoring.drawdown_monitor import DrawdownMonitor
from maais.monitoring.health import SystemHealthTracker
from maais.monitoring.kill_switch import KillSwitch
from maais.monitoring.schemas import ComponentName

logger = get_logger(__name__)


class MonitoringEngine:
    """Central monitoring orchestrator."""

    def __init__(
        self,
        kill_switch: KillSwitch,
        black_swan_guard: BlackSwanGuard,
        drawdown_monitor: DrawdownMonitor,
        alert_dispatcher: AlertDispatcher,
        health_tracker: SystemHealthTracker,
    ) -> None:
        self._kill_switch = kill_switch
        self._black_swan = black_swan_guard
        self._drawdown_monitor = drawdown_monitor
        self._alerts = alert_dispatcher
        self._health = health_tracker

    async def is_trading_allowed(
        self,
        features: FeatureSet,
        total_exposure_pct: float = 0.0,
    ) -> tuple[bool, str | None]:
        """Run all pre-trade monitoring checks.

        Rule 20: this method CANNOT be bypassed — callers must check the
        return value and not proceed if allowed=False.

        Returns:
            (allowed: bool, reason: str | None)
        """
        # 1. Kill-switch check (Rule 17)
        if self._kill_switch.is_halted():
            reason = f"kill_switch_active: {self._kill_switch.halt_reason}"
            logger.warning("trading_blocked_kill_switch", reason=reason)
            return False, reason

        # 2. Black swan check (Rule 18)
        bs_allowed, bs_reason = self._black_swan.check(features, total_exposure_pct)
        if not bs_allowed:
            await self._alerts.send_critical(
                component=ComponentName.MONITORING,
                title="Black Swan Protection Triggered",
                message=bs_reason or "unknown",
                symbol=features.symbol,
            )
            return False, bs_reason

        # 3. Drawdown check — fires alerts, may trigger kill-switch
        await self._drawdown_monitor.check()

        # After drawdown check, re-verify kill-switch (drawdown check may have triggered it)
        if self._kill_switch.is_halted():
            reason = f"kill_switch_active_post_drawdown: {self._kill_switch.halt_reason}"
            return False, reason

        return True, None

    def ping_component(self, component: str) -> None:
        """Record a healthy heartbeat for a component."""
        self._health.ping(component)

    def record_component_error(self, component: str, error: str) -> None:
        """Record an error for a component."""
        self._health.record_error(component, error)

    def all_components_healthy(self) -> bool:
        return self._health.all_healthy()

    def unhealthy_components(self) -> list[str]:
        return self._health.unhealthy_components()

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def black_swan_guard(self) -> BlackSwanGuard:
        return self._black_swan
