"""Tests for the Risk Engine (Batch 6)."""

from datetime import datetime, timezone

import pytest

from maais.config.constants import (
    CORRELATION_FULL_THRESHOLD,
    CORRELATION_REDUCE_20_THRESHOLD,
    CORRELATION_REDUCE_40_THRESHOLD,
    CORRELATION_ROLLING_WINDOW,
    DRAWDOWN_HALT_THRESHOLD,
    DRAWDOWN_REDUCE_25_THRESHOLD,
    DRAWDOWN_REDUCE_50_THRESHOLD,
    MAX_RISK_PER_TRADE_PCT,
    PORTFOLIO_RISK_LIMIT_PCT,
)
from maais.decision.schemas import EVResult
from maais.feature_pipeline.features import FeatureSet
from maais.risk.correlation import CorrelationController, _correlation_multiplier
from maais.risk.drawdown import DrawdownController, _get_multiplier
from maais.risk.engine import RiskEngine
from maais.risk.kelly import half_kelly_fraction, volatility_adjusted_fraction
from maais.risk.portfolio import PortfolioRiskLimiter

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts():
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _features(atr: float | None = 500.0, zscore_mean: float | None = 50000.0) -> FeatureSet:
    return FeatureSet(
        symbol="BTCUSDT",
        timeframe="1m",
        timestamp=_ts(),
        atr=atr,
        zscore_mean=zscore_mean,
    )


def _ev(p_win: float = 0.65, gain: float = 0.01, loss: float = 0.01) -> EVResult:
    net = p_win * gain - (1 - p_win) * loss - 0.002
    return EVResult(
        p_win=p_win,
        p_loss=1 - p_win,
        expected_gain_pct=gain,
        expected_loss_pct=loss,
        gross_ev=p_win * gain - (1 - p_win) * loss,
        funding_carry=0.0,
        net_ev=net,
        is_positive=net > 0,
    )


def _engine(capital: float = 100_000.0) -> RiskEngine:
    return RiskEngine(
        drawdown_controller=DrawdownController(capital),
        correlation_controller=CorrelationController(),
        portfolio_limiter=PortfolioRiskLimiter(),
    )


# ── Constants ─────────────────────────────────────────────────────────────────


class TestConstants:
    def test_correlation_rolling_window_is_60(self):
        assert CORRELATION_ROLLING_WINDOW == 60

    def test_drawdown_thresholds(self):
        assert DRAWDOWN_REDUCE_25_THRESHOLD == pytest.approx(0.05)
        assert DRAWDOWN_REDUCE_50_THRESHOLD == pytest.approx(0.10)
        assert DRAWDOWN_HALT_THRESHOLD == pytest.approx(0.20)

    def test_correlation_thresholds(self):
        assert CORRELATION_FULL_THRESHOLD == pytest.approx(0.30)
        assert CORRELATION_REDUCE_20_THRESHOLD == pytest.approx(0.60)
        assert CORRELATION_REDUCE_40_THRESHOLD == pytest.approx(0.70)

    def test_max_risk_per_trade(self):
        assert MAX_RISK_PER_TRADE_PCT == pytest.approx(0.02)

    def test_portfolio_risk_limit(self):
        assert PORTFOLIO_RISK_LIMIT_PCT == pytest.approx(0.15)


# ── Half-Kelly ────────────────────────────────────────────────────────────────


class TestHalfKelly:
    def test_coin_flip_zero_kelly(self):
        # p=0.5, b=1.0 → full kelly = 0 → half kelly = 0
        frac = half_kelly_fraction(0.5, 0.01, 0.01)
        assert frac == pytest.approx(0.0)

    def test_positive_edge_positive_fraction(self):
        frac = half_kelly_fraction(0.6, 0.01, 0.01)
        assert frac > 0

    def test_higher_probability_larger_fraction(self):
        low = half_kelly_fraction(0.55, 0.01, 0.01)
        high = half_kelly_fraction(0.70, 0.01, 0.01)
        assert high > low

    def test_negative_edge_zero(self):
        # p=0.4 → full kelly negative → return 0
        frac = half_kelly_fraction(0.4, 0.01, 0.01)
        assert frac == pytest.approx(0.0)

    def test_capped_at_25_percent(self):
        # Extreme edge → capped at 0.25
        frac = half_kelly_fraction(0.99, 0.10, 0.01)
        assert frac <= 0.25

    def test_zero_gain_returns_zero(self):
        assert half_kelly_fraction(0.7, 0.0, 0.01) == 0.0

    def test_zero_loss_returns_zero(self):
        assert half_kelly_fraction(0.7, 0.01, 0.0) == 0.0


# ── Volatility-Adjusted Sizing ────────────────────────────────────────────────


class TestVolatilityAdjusted:
    def test_higher_atr_smaller_fraction(self):
        # ATR=2000 → atr_pct=0.04 → fraction=0.5 → capped at 0.25
        # ATR=5000 → atr_pct=0.10 → fraction=0.2 → not capped
        low_vol = volatility_adjusted_fraction(2000.0, 50000.0)
        high_vol = volatility_adjusted_fraction(5000.0, 50000.0)
        assert high_vol < low_vol

    def test_none_atr_returns_fallback(self):
        frac = volatility_adjusted_fraction(None, 50000.0)
        assert frac == pytest.approx(MAX_RISK_PER_TRADE_PCT)

    def test_none_price_returns_fallback(self):
        frac = volatility_adjusted_fraction(500.0, None)
        assert frac == pytest.approx(MAX_RISK_PER_TRADE_PCT)

    def test_capped_at_25_percent(self):
        # Very low ATR → would be huge fraction → capped
        frac = volatility_adjusted_fraction(1.0, 50000.0)
        assert frac <= 0.25

    def test_fraction_is_positive(self):
        frac = volatility_adjusted_fraction(500.0, 50000.0)
        assert frac > 0


# ── Drawdown Controller ───────────────────────────────────────────────────────


class TestDrawdownController:
    def test_no_drawdown_normal_multiplier(self):
        ctrl = DrawdownController(100_000.0)
        state = ctrl.get_state()
        assert state.multiplier == pytest.approx(1.0)
        assert not state.is_halted
        assert state.drawdown_pct == pytest.approx(0.0)

    def test_5_percent_drawdown_reduces_25(self):
        ctrl = DrawdownController(100_000.0)
        ctrl.update(94_000.0)  # 6% drawdown
        state = ctrl.get_state()
        assert state.multiplier == pytest.approx(0.75)

    def test_10_percent_drawdown_reduces_50(self):
        ctrl = DrawdownController(100_000.0)
        ctrl.update(89_000.0)  # 11% drawdown
        state = ctrl.get_state()
        assert state.multiplier == pytest.approx(0.50)

    def test_20_percent_drawdown_halts(self):
        ctrl = DrawdownController(100_000.0)
        ctrl.update(79_000.0)  # 21% drawdown
        state = ctrl.get_state()
        assert state.multiplier == pytest.approx(0.0)
        assert state.is_halted

    def test_peak_advances_on_new_high(self):
        ctrl = DrawdownController(100_000.0)
        ctrl.update(110_000.0)
        assert ctrl.peak_capital == pytest.approx(110_000.0)

    def test_peak_does_not_retreat(self):
        ctrl = DrawdownController(100_000.0)
        ctrl.update(110_000.0)
        ctrl.update(90_000.0)
        assert ctrl.peak_capital == pytest.approx(110_000.0)

    def test_drawdown_calculated_from_peak(self):
        ctrl = DrawdownController(100_000.0)
        ctrl.update(120_000.0)  # new peak
        ctrl.update(96_000.0)  # 20% down from peak of 120k → 0.2
        state = ctrl.get_state()
        assert state.drawdown_pct == pytest.approx(0.20, rel=1e-4)
        assert state.is_halted

    def test_get_multiplier_helper_below_5(self):
        mult, halted = _get_multiplier(0.03)
        assert mult == 1.0 and not halted

    def test_get_multiplier_helper_5_to_10(self):
        mult, halted = _get_multiplier(0.07)
        assert mult == 0.75 and not halted

    def test_get_multiplier_helper_10_to_20(self):
        mult, halted = _get_multiplier(0.12)
        assert mult == 0.50 and not halted

    def test_get_multiplier_helper_above_20(self):
        mult, halted = _get_multiplier(0.25)
        assert mult == 0.0 and halted


# ── Correlation Controller ────────────────────────────────────────────────────


class TestCorrelationController:
    def _fill(self, ctrl: CorrelationController, sym: str, returns: list[float]) -> None:
        for r in returns:
            ctrl.add_return(sym, r)

    def test_insufficient_data_returns_none(self):
        ctrl = CorrelationController()
        ctrl.add_return("BTCUSDT", 0.01)
        assert ctrl.get_correlation("BTCUSDT", "ETHUSDT") is None

    def test_perfect_correlation(self):
        ctrl = CorrelationController()
        returns = [0.01 * i for i in range(1, 61)]
        self._fill(ctrl, "BTCUSDT", returns)
        self._fill(ctrl, "ETHUSDT", returns)
        corr = ctrl.get_correlation("BTCUSDT", "ETHUSDT")
        assert corr == pytest.approx(1.0, rel=1e-4)

    def test_perfect_negative_correlation(self):
        ctrl = CorrelationController()
        returns = [0.01 * i for i in range(1, 61)]
        self._fill(ctrl, "BTCUSDT", returns)
        self._fill(ctrl, "ETHUSDT", [-r for r in returns])
        corr = ctrl.get_correlation("BTCUSDT", "ETHUSDT")
        assert corr == pytest.approx(-1.0, rel=1e-4)

    def test_unknown_symbol_multiplier_is_1(self):
        ctrl = CorrelationController()
        assert ctrl.get_multiplier("BTCUSDT", "ETHUSDT") == pytest.approx(1.0)

    def test_low_correlation_full_allocation(self):
        assert _correlation_multiplier(0.10) == pytest.approx(1.0)

    def test_medium_correlation_reduce_20(self):
        assert _correlation_multiplier(0.45) == pytest.approx(0.80)

    def test_high_correlation_reduce_40(self):
        assert _correlation_multiplier(0.65) == pytest.approx(0.60)

    def test_very_high_correlation_blocked(self):
        assert _correlation_multiplier(0.75) == pytest.approx(0.0)

    def test_at_full_threshold_reduces(self):
        # exactly 0.30 → reduce 20%
        assert _correlation_multiplier(0.30) == pytest.approx(0.80)

    def test_rolling_window_limits_history(self):
        ctrl = CorrelationController(window=5)
        for i in range(10):
            ctrl.add_return("BTCUSDT", float(i))
        assert len(ctrl._returns["BTCUSDT"]) == 5

    def test_symbols_tracked(self):
        ctrl = CorrelationController()
        ctrl.add_return("BTCUSDT", 0.01)
        ctrl.add_return("ETHUSDT", 0.02)
        assert set(ctrl.symbols_tracked()) == {"BTCUSDT", "ETHUSDT"}


# ── Portfolio Risk Limiter ────────────────────────────────────────────────────


class TestPortfolioRiskLimiter:
    def test_empty_portfolio_allows_new_position(self):
        limiter = PortfolioRiskLimiter()
        allowed, reason = limiter.can_add_position("BTCUSDT", 5_000.0, 100_000.0)
        assert allowed
        assert reason is None

    def test_exposure_within_cap_allowed(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("ETHUSDT", 5_000.0)
        allowed, _ = limiter.can_add_position("BTCUSDT", 5_000.0, 100_000.0)
        assert allowed  # 10% total < 15% cap

    def test_exposure_exceeding_cap_rejected(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("ETHUSDT", 12_000.0)  # 12%
        allowed, reason = limiter.can_add_position("BTCUSDT", 5_000.0, 100_000.0)
        assert not allowed  # 17% > 15%
        assert reason is not None

    def test_total_exposure_usd(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("BTCUSDT", 3_000.0)
        limiter.add_position("ETHUSDT", 4_000.0)
        assert limiter.total_exposure_usd() == pytest.approx(7_000.0)

    def test_total_exposure_pct(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("BTCUSDT", 10_000.0)
        assert limiter.total_exposure_pct(100_000.0) == pytest.approx(0.10)

    def test_remove_position_reduces_exposure(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("BTCUSDT", 10_000.0)
        limiter.remove_position("BTCUSDT")
        assert limiter.total_exposure_usd() == pytest.approx(0.0)

    def test_replacing_existing_position_uses_new_size(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("BTCUSDT", 14_000.0)  # 14%
        # Replace with larger → would push to 18% → rejected
        allowed, _ = limiter.can_add_position("BTCUSDT", 18_000.0, 100_000.0)
        assert not allowed

    def test_open_positions_dict(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("BTCUSDT", 5_000.0)
        assert "BTCUSDT" in limiter.open_positions()


# ── Risk Engine (Integration) ──────────────────────────────────────────────────


class TestRiskEngine:
    def test_normal_conditions_approved(self):
        engine = _engine()
        result = engine.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features())
        assert result.approved
        assert result.final_usd > 0

    def test_final_size_rule14_formula(self):
        # final = min(half_kelly, vol_adjusted, risk_cap) * dd_mult * corr_mult
        engine = _engine()
        result = engine.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features())
        expected_base = min(
            result.half_kelly_fraction,
            result.vol_adjusted_fraction,
            result.risk_cap_fraction,
        )
        assert result.base_fraction == pytest.approx(expected_base)

    def test_drawdown_halt_blocks_trade(self):
        dd = DrawdownController(100_000.0)
        dd.update(75_000.0)  # 25% drawdown → halt
        engine = RiskEngine(dd, CorrelationController(), PortfolioRiskLimiter())
        result = engine.evaluate("BTCUSDT", 75_000.0, _ev(0.65), _features())
        assert not result.approved
        assert "drawdown_halt" in result.rejection_reason

    def test_drawdown_5pct_reduces_size(self):
        dd_normal = DrawdownController(100_000.0)
        dd_reduced = DrawdownController(100_000.0)
        dd_reduced.update(94_000.0)  # 6% drawdown → 0.75 multiplier

        engine_normal = RiskEngine(dd_normal, CorrelationController(), PortfolioRiskLimiter())
        engine_reduced = RiskEngine(dd_reduced, CorrelationController(), PortfolioRiskLimiter())

        r_normal = engine_normal.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features())
        r_reduced = engine_reduced.evaluate("BTCUSDT", 94_000.0, _ev(0.65), _features())

        assert r_reduced.drawdown_multiplier == pytest.approx(0.75)
        assert r_reduced.final_fraction < r_normal.final_fraction

    def test_high_correlation_blocks_trade(self):
        ctrl = CorrelationController()
        # Add highly correlated return series
        returns = [0.01 * i for i in range(1, 61)]
        for r in returns:
            ctrl.add_return("BTCUSDT", r)
            ctrl.add_return("ETHUSDT", r)  # perfect correlation

        engine = RiskEngine(DrawdownController(100_000.0), ctrl, PortfolioRiskLimiter())
        result = engine.evaluate(
            "ETHUSDT",
            100_000.0,
            _ev(0.65),
            _features(),
            open_symbols=["BTCUSDT"],
        )
        assert not result.approved
        assert "correlation_cluster" in result.rejection_reason

    def test_portfolio_cap_blocks_trade(self):
        limiter = PortfolioRiskLimiter()
        limiter.add_position("ETHUSDT", 14_500.0)  # 14.5% of 100k
        engine = RiskEngine(DrawdownController(100_000.0), CorrelationController(), limiter)
        result = engine.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features())
        assert not result.approved
        assert "portfolio_cap" in result.rejection_reason

    def test_result_fields_populated(self):
        engine = _engine()
        result = engine.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features())
        assert result.symbol == "BTCUSDT"
        assert result.capital == pytest.approx(100_000.0)
        assert result.half_kelly_fraction >= 0
        assert result.vol_adjusted_fraction > 0
        assert result.risk_cap_fraction == pytest.approx(MAX_RISK_PER_TRADE_PCT)
        assert result.drawdown_multiplier == pytest.approx(1.0)
        assert result.correlation_multiplier == pytest.approx(1.0)

    def test_no_open_symbols_no_correlation_penalty(self):
        engine = _engine()
        result = engine.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features(), open_symbols=[])
        assert result.correlation_multiplier == pytest.approx(1.0)

    def test_final_usd_equals_fraction_times_capital(self):
        engine = _engine()
        result = engine.evaluate("BTCUSDT", 100_000.0, _ev(0.65), _features())
        assert result.final_usd == pytest.approx(result.final_fraction * result.capital)
