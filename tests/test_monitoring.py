"""Tests for the Monitoring Layer (Batch 8)."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from maais.config.constants import (
    DRAWDOWN_HALT_THRESHOLD,
    DRAWDOWN_REDUCE_25_THRESHOLD,
    DRAWDOWN_REDUCE_50_THRESHOLD,
    HIGH_VOL_MULTIPLIER,
)
from maais.feature_pipeline.features import FeatureSet
from maais.monitoring.alerting import AlertDispatcher
from maais.monitoring.black_swan import BlackSwanGuard, _LIQUIDITY_COLLAPSE_SPREAD, _EXPOSURE_HARD_CEILING
from maais.monitoring.drawdown_monitor import DrawdownMonitor
from maais.monitoring.engine import MonitoringEngine
from maais.monitoring.health import SystemHealthTracker
from maais.monitoring.kill_switch import KillSwitch
from maais.monitoring.schemas import AlertEvent, AlertLevel, ComponentName, HealthStatus
from maais.risk.drawdown import DrawdownController


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _features(
    rolling_std: float | None = 0.001,
    bid_ask_spread: float | None = 0.001,
    symbol: str = "BTCUSDT",
) -> FeatureSet:
    return FeatureSet(
        symbol=symbol,
        timeframe="1m",
        timestamp=_ts(),
        rolling_std=rolling_std,
        bid_ask_spread=bid_ask_spread,
    )


def _dispatcher(telegram: bool = False) -> AlertDispatcher:
    if telegram:
        return AlertDispatcher(telegram_token="tok", telegram_chat_id="123")
    return AlertDispatcher()


def _engine(capital: float = 100_000.0, drawdown_pct: float = 0.0) -> MonitoringEngine:
    current = capital * (1 - drawdown_pct)
    dd_ctrl = DrawdownController(capital)
    if drawdown_pct > 0:
        dd_ctrl.update(current)
    kill = KillSwitch()
    dispatcher = AlertDispatcher()
    dd_monitor = DrawdownMonitor(dd_ctrl, dispatcher, kill)
    bs_guard = BlackSwanGuard()
    health = SystemHealthTracker()
    return MonitoringEngine(kill, bs_guard, dd_monitor, dispatcher, health)


# ── AlertEvent / schemas ──────────────────────────────────────────────────────

class TestSchemas:
    def test_alert_level_values(self):
        assert AlertLevel.INFO.value == "INFO"
        assert AlertLevel.WARNING.value == "WARNING"
        assert AlertLevel.CRITICAL.value == "CRITICAL"

    def test_component_name_values(self):
        assert ComponentName.MARKET_DATA.value == "market_data"
        assert ComponentName.EXECUTION.value == "execution"

    def test_alert_event_has_timestamp(self):
        event = AlertEvent(AlertLevel.INFO, "test", "title", "message")
        assert event.timestamp is not None

    def test_health_status_defaults(self):
        status = HealthStatus("test_component", is_healthy=True, last_heartbeat=None)
        assert status.error_count == 0
        assert status.last_error is None


# ── AlertDispatcher ───────────────────────────────────────────────────────────

class TestAlertDispatcher:
    def test_telegram_disabled_by_default(self):
        dispatcher = AlertDispatcher()
        assert not dispatcher.telegram_enabled

    def test_telegram_enabled_with_credentials(self):
        dispatcher = AlertDispatcher(telegram_token="tok", telegram_chat_id="123")
        assert dispatcher.telegram_enabled

    def test_telegram_disabled_missing_chat_id(self):
        dispatcher = AlertDispatcher(telegram_token="tok")
        assert not dispatcher.telegram_enabled

    async def test_send_logs_without_telegram(self):
        dispatcher = AlertDispatcher()
        event = AlertEvent(AlertLevel.WARNING, "test", "Title", "Message")
        # Should not raise
        await dispatcher.send(event)

    async def test_send_critical_convenience(self):
        dispatcher = AlertDispatcher()
        await dispatcher.send_critical("risk", "Drawdown Alert", "10% drawdown")

    async def test_send_warning_convenience(self):
        dispatcher = AlertDispatcher()
        await dispatcher.send_warning("execution", "Fill Failed", "Order rejected")

    async def test_send_info_convenience(self):
        dispatcher = AlertDispatcher()
        await dispatcher.send_info("market_data", "Connected", "Binance WS connected")

    async def test_telegram_failure_does_not_raise(self):
        dispatcher = AlertDispatcher(telegram_token="bad_token", telegram_chat_id="123")
        event = AlertEvent(AlertLevel.CRITICAL, "test", "Title", "Message")
        # Even if Telegram fails, should not propagate exception
        await dispatcher.send(event)


# ── SystemHealthTracker ───────────────────────────────────────────────────────

class TestSystemHealthTracker:
    def test_unseen_component_is_unhealthy(self):
        tracker = SystemHealthTracker()
        status = tracker.get_status("unknown")
        assert not status.is_healthy

    def test_ping_marks_healthy(self):
        tracker = SystemHealthTracker()
        tracker.ping("market_data")
        assert tracker.get_status("market_data").is_healthy

    def test_all_healthy_empty(self):
        tracker = SystemHealthTracker()
        assert tracker.all_healthy()

    def test_all_healthy_after_pings(self):
        tracker = SystemHealthTracker()
        tracker.ping("market_data")
        tracker.ping("execution")
        assert tracker.all_healthy()

    def test_record_error_marks_unhealthy(self):
        tracker = SystemHealthTracker()
        tracker.ping("execution")
        tracker.record_error("execution", "connection lost")
        assert not tracker.get_status("execution").is_healthy

    def test_error_increments_count(self):
        tracker = SystemHealthTracker()
        tracker.record_error("risk", "error1")
        tracker.record_error("risk", "error2")
        assert tracker.get_status("risk").error_count == 2

    def test_stale_heartbeat_marks_unhealthy(self):
        tracker = SystemHealthTracker(stale_seconds=0)
        tracker.ping("market_data")
        # With stale_seconds=0, immediately stale
        import time; time.sleep(0.01)
        assert not tracker.get_status("market_data").is_healthy

    def test_unhealthy_components_lists_bad_ones(self):
        tracker = SystemHealthTracker()
        tracker.ping("market_data")
        tracker.record_error("execution", "fail")
        assert "execution" in tracker.unhealthy_components()
        assert "market_data" not in tracker.unhealthy_components()


# ── KillSwitch ────────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_initially_not_halted(self):
        ks = KillSwitch()
        assert not ks.is_halted()

    def test_halt_sets_flag(self):
        ks = KillSwitch()
        ks.halt("test reason")
        assert ks.is_halted()

    def test_halt_stores_reason(self):
        ks = KillSwitch()
        ks.halt("drawdown_exceeded")
        assert ks.halt_reason == "drawdown_exceeded"

    def test_halt_increments_count(self):
        ks = KillSwitch()
        ks.halt("r1")
        ks.halt("r2")
        assert ks.halt_count == 2

    def test_halt_sets_time(self):
        ks = KillSwitch()
        ks.halt("test")
        assert ks.halt_time is not None

    def test_reset_with_correct_code(self):
        ks = KillSwitch()
        ks.halt("test")
        ok, msg = ks.reset(ks.reset_reason_code)
        assert ok
        assert not ks.is_halted()

    def test_reset_with_wrong_code_rejected(self):
        ks = KillSwitch()
        ks.halt("test")
        ok, msg = ks.reset("wrong_code")
        assert not ok
        assert ks.is_halted()  # still halted
        assert "Rule 20" in msg

    def test_reset_when_not_halted(self):
        ks = KillSwitch()
        ok, msg = ks.reset(ks.reset_reason_code)
        assert ok
        assert "not_active" in msg

    def test_multiple_halts_require_one_reset(self):
        ks = KillSwitch()
        ks.halt("r1")
        ks.halt("r2")
        ks.reset(ks.reset_reason_code)
        assert not ks.is_halted()


# ── BlackSwanGuard ────────────────────────────────────────────────────────────

class TestBlackSwanGuard:
    def test_normal_conditions_allowed(self):
        guard = BlackSwanGuard(baseline_std=0.002)
        feats = _features(rolling_std=0.001, bid_ask_spread=0.001)
        ok, reason = guard.check(feats)
        assert ok
        assert reason is None

    def test_volatility_circuit_breaker(self):
        guard = BlackSwanGuard(baseline_std=0.002, vol_multiplier=1.5)
        # rolling_std = 0.004 > 0.002 × 1.5 = 0.003 → trigger
        feats = _features(rolling_std=0.004)
        ok, reason = guard.check(feats)
        assert not ok
        assert "volatility_circuit_breaker" in reason

    def test_volatility_just_below_threshold_allowed(self):
        guard = BlackSwanGuard(baseline_std=0.002, vol_multiplier=1.5)
        feats = _features(rolling_std=0.0029)  # just below 0.003
        ok, _ = guard.check(feats)
        assert ok

    def test_liquidity_collapse(self):
        guard = BlackSwanGuard()
        feats = _features(bid_ask_spread=0.06)  # > 0.05 threshold
        ok, reason = guard.check(feats)
        assert not ok
        assert "liquidity_collapse" in reason

    def test_exposure_ceiling_breach(self):
        guard = BlackSwanGuard()
        feats = _features(rolling_std=0.001, bid_ask_spread=0.001)
        ok, reason = guard.check(feats, total_exposure_pct=0.30)  # > 0.25
        assert not ok
        assert "exposure_ceiling" in reason

    def test_none_rolling_std_skips_vol_check(self):
        guard = BlackSwanGuard()
        feats = _features(rolling_std=None, bid_ask_spread=0.001)
        ok, _ = guard.check(feats)
        assert ok

    def test_none_spread_skips_liquidity_check(self):
        guard = BlackSwanGuard()
        feats = _features(rolling_std=0.001, bid_ask_spread=None)
        ok, _ = guard.check(feats)
        assert ok

    def test_properties(self):
        guard = BlackSwanGuard(baseline_std=0.003, vol_multiplier=2.0)
        assert guard.baseline_std == pytest.approx(0.003)
        assert guard.vol_multiplier == pytest.approx(2.0)
        assert guard.liquidity_threshold == pytest.approx(_LIQUIDITY_COLLAPSE_SPREAD)
        assert guard.exposure_ceiling == pytest.approx(_EXPOSURE_HARD_CEILING)


# ── DrawdownMonitor ───────────────────────────────────────────────────────────

class TestDrawdownMonitor:
    def _monitor(self, capital: float, current: float):
        dd_ctrl = DrawdownController(capital)
        if current < capital:
            dd_ctrl.update(current)
        dispatcher = AlertDispatcher()
        kill = KillSwitch()
        dispatcher.send_warning = AsyncMock()
        dispatcher.send_critical = AsyncMock()
        monitor = DrawdownMonitor(dd_ctrl, dispatcher, kill)
        return monitor, dispatcher, kill

    async def test_no_drawdown_no_alert(self):
        monitor, dispatcher, _ = self._monitor(100_000, 100_000)
        await monitor.check()
        dispatcher.send_warning.assert_not_called()
        dispatcher.send_critical.assert_not_called()

    async def test_5pct_drawdown_warning(self):
        monitor, dispatcher, _ = self._monitor(100_000, 94_000)  # 6% drawdown
        await monitor.check()
        dispatcher.send_warning.assert_called_once()

    async def test_10pct_drawdown_warning(self):
        monitor, dispatcher, _ = self._monitor(100_000, 89_000)  # 11% drawdown
        await monitor.check()
        dispatcher.send_warning.assert_called_once()

    async def test_15pct_drawdown_critical(self):
        monitor, dispatcher, _ = self._monitor(100_000, 84_000)  # 16% drawdown
        await monitor.check()
        dispatcher.send_critical.assert_called_once()

    async def test_20pct_drawdown_triggers_kill_switch(self):
        monitor, dispatcher, kill = self._monitor(100_000, 79_000)  # 21% drawdown
        await monitor.check()
        assert kill.is_halted()
        dispatcher.send_critical.assert_called_once()

    async def test_no_duplicate_alerts_same_tier(self):
        monitor, dispatcher, _ = self._monitor(100_000, 94_000)  # 6% drawdown
        await monitor.check()
        await monitor.check()
        # Second check at same tier should not re-alert
        assert dispatcher.send_warning.call_count == 1


# ── MonitoringEngine (integration) ───────────────────────────────────────────

class TestMonitoringEngine:
    async def test_normal_conditions_trading_allowed(self):
        engine = _engine()
        allowed, reason = await engine.is_trading_allowed(_features())
        assert allowed
        assert reason is None

    async def test_kill_switch_blocks_trading(self):
        engine = _engine()
        engine.kill_switch.halt("manual_test")
        allowed, reason = await engine.is_trading_allowed(_features())
        assert not allowed
        assert "kill_switch_active" in reason

    async def test_black_swan_vol_blocks_trading(self):
        engine = _engine()
        # High volatility — rolling_std >> baseline
        feats = _features(rolling_std=0.05)  # very high
        allowed, reason = await engine.is_trading_allowed(feats)
        assert not allowed
        assert "volatility_circuit_breaker" in reason

    async def test_black_swan_spread_blocks_trading(self):
        engine = _engine()
        feats = _features(bid_ask_spread=0.10)  # liquidity collapse
        allowed, reason = await engine.is_trading_allowed(feats)
        assert not allowed
        assert "liquidity_collapse" in reason

    async def test_drawdown_halt_blocks_trading(self):
        engine = _engine(capital=100_000, drawdown_pct=0.22)  # 22% drawdown → halt
        allowed, reason = await engine.is_trading_allowed(_features())
        assert not allowed

    async def test_ping_component_records_health(self):
        engine = _engine()
        engine.ping_component("market_data")
        assert engine.all_components_healthy()

    async def test_record_error_marks_unhealthy(self):
        engine = _engine()
        engine.ping_component("execution")
        engine.record_component_error("execution", "connection lost")
        assert "execution" in engine.unhealthy_components()

    async def test_exposure_ceiling_blocks(self):
        engine = _engine()
        allowed, reason = await engine.is_trading_allowed(_features(), total_exposure_pct=0.30)
        assert not allowed
        assert "exposure_ceiling" in reason
