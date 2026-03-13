"""Tests for the Feature Engineering Pipeline (Batch 3)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from maais.config.constants import ZSCORE_TRIGGER, Regime
from maais.feature_pipeline.funding_features import compute_funding_features
from maais.feature_pipeline.momentum import compute_ema, compute_momentum_features, compute_roc
from maais.feature_pipeline.orderbook_features import compute_orderbook_features
from maais.feature_pipeline.pipeline import FeaturePipeline
from maais.feature_pipeline.regime_classifier import classify_regime
from maais.feature_pipeline.volatility import compute_atr, compute_rolling_std
from maais.feature_pipeline.zscore import compute_zscore, is_mean_reversion_signal
from maais.market_data.schemas import FundingRateData, KlineData, OrderBookSnapshot


# ── Helpers ───────────────────────────────────────────────────────────────────

def _candle(minute: int, close: float, high: float | None = None, low: float | None = None) -> KlineData:
    ot = datetime(2024, 1, 1, 0, minute % 60, 0, tzinfo=timezone.utc)
    ct = datetime(2024, 1, 1, 0, minute % 60, 59, tzinfo=timezone.utc)
    c = Decimal(str(close))
    return KlineData(
        symbol="BTCUSDT", timeframe="1m",
        open_time=ot, open=c,
        high=Decimal(str(high or close + 0.5)),
        low=Decimal(str(low or close - 0.5)),
        close=c, volume=Decimal("10"),
        close_time=ct, quote_volume=Decimal("1000"),
        trade_count=50, taker_buy_volume=Decimal("5"),
        taker_buy_quote_volume=Decimal("500"), is_closed=True,
    )


def _candles(prices: list[float]) -> list[KlineData]:
    return [_candle(i, p) for i, p in enumerate(prices)]


# ── Z-score ───────────────────────────────────────────────────────────────────

class TestZscore:
    def test_insufficient_data_returns_none(self):
        z, mean, std = compute_zscore([100.0] * 5, lookback=20)
        assert z is None and mean is None and std is None

    def test_flat_series_zero_zscore(self):
        z, _, _ = compute_zscore([100.0] * 20)
        assert z == 0.0

    def test_price_above_mean_positive_zscore(self):
        prices = [100.0] * 19 + [110.0]
        z, _, _ = compute_zscore(prices)
        assert z is not None and z > 0

    def test_price_below_mean_negative_zscore(self):
        prices = [100.0] * 19 + [90.0]
        z, _, _ = compute_zscore(prices)
        assert z is not None and z < 0

    def test_mean_reversion_signal_above_trigger(self):
        # Spike far above mean → |Z| > 3
        prices = [100.0] * 19 + [200.0]
        z, _, _ = compute_zscore(prices, lookback=20)
        assert is_mean_reversion_signal(z)

    def test_mean_reversion_signal_false_within_range(self):
        prices = [100.0 + i * 0.01 for i in range(20)]
        z, _, _ = compute_zscore(prices)
        assert not is_mean_reversion_signal(z)

    def test_mean_reversion_signal_none_input(self):
        assert not is_mean_reversion_signal(None)

    def test_zscore_trigger_constant_is_3(self):
        assert ZSCORE_TRIGGER == 3.0


# ── Momentum ──────────────────────────────────────────────────────────────────

class TestEma:
    def test_insufficient_data_returns_none(self):
        assert compute_ema([100.0] * 5, period=9) is None

    def test_flat_prices_ema_equals_price(self):
        ema = compute_ema([100.0] * 20, period=9)
        assert ema == pytest.approx(100.0, rel=1e-6)

    def test_rising_prices_ema_below_last_price(self):
        prices = [float(i) for i in range(1, 25)]
        ema = compute_ema(prices, period=9)
        assert ema < prices[-1]

    def test_ema_uses_exponential_weighting(self):
        # With recent spike, EMA should be pulled toward it
        prices = [100.0] * 9 + [200.0]
        ema9 = compute_ema(prices, 9)
        assert ema9 > 100.0


class TestRoc:
    def test_insufficient_data_returns_none(self):
        assert compute_roc([100.0] * 3, period=5) is None

    def test_flat_prices_zero_roc(self):
        assert compute_roc([100.0] * 10, period=5) == pytest.approx(0.0)

    def test_10_percent_gain(self):
        prices = [100.0] + [110.0] * 5
        roc = compute_roc(prices, period=5)
        assert roc == pytest.approx(0.10, rel=1e-6)

    def test_10_percent_loss(self):
        prices = [100.0] + [90.0] * 5
        roc = compute_roc(prices, period=5)
        assert roc == pytest.approx(-0.10, rel=1e-6)


class TestMomentumFeatures:
    def test_returns_all_keys(self):
        prices = [100.0 + i for i in range(30)]
        result = compute_momentum_features(prices)
        assert set(result.keys()) == {"ema_fast", "ema_slow", "ema_signal", "roc_short", "roc_long"}

    def test_ema_signal_is_difference(self):
        prices = [100.0 + i for i in range(30)]
        result = compute_momentum_features(prices)
        if result["ema_fast"] and result["ema_slow"]:
            assert result["ema_signal"] == pytest.approx(result["ema_fast"] - result["ema_slow"])


# ── Volatility ────────────────────────────────────────────────────────────────

class TestAtr:
    def test_insufficient_data_returns_none(self):
        candles = _candles([100.0] * 5)
        assert compute_atr(candles, period=14) is None

    def test_flat_market_low_atr(self):
        candles = [_candle(i, 100.0, high=100.5, low=99.5) for i in range(20)]
        atr = compute_atr(candles)
        assert atr == pytest.approx(1.0, rel=1e-4)

    def test_atr_is_positive(self):
        candles = _candles([float(100 + i % 5) for i in range(20)])
        atr = compute_atr(candles)
        assert atr is not None and atr > 0


class TestRollingStd:
    def test_insufficient_data_returns_none(self):
        assert compute_rolling_std([100.0] * 5, period=14) is None

    def test_flat_prices_near_zero_std(self):
        prices = [100.0] * 20
        std = compute_rolling_std(prices)
        assert std == pytest.approx(0.0, abs=1e-10)

    def test_volatile_prices_higher_std(self):
        stable = [100.0] * 20
        volatile = [100.0 + (i % 2) * 10.0 for i in range(20)]
        assert compute_rolling_std(volatile) > compute_rolling_std(stable)


# ── Order Book ────────────────────────────────────────────────────────────────

class TestOrderbookFeatures:
    def _snapshot(self, bids, asks) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            symbol="BTCUSDT",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            bids=[(Decimal(str(p)), Decimal(str(q))) for p, q in bids],
            asks=[(Decimal(str(p)), Decimal(str(q))) for p, q in asks],
            last_update_id=1,
        )

    def test_none_snapshot_returns_nones(self):
        result = compute_orderbook_features(None)
        assert result["bid_ask_spread"] is None
        assert result["book_imbalance"] is None

    def test_spread_calculation(self):
        snap = self._snapshot([(99.0, 1.0)], [(101.0, 1.0)])
        result = compute_orderbook_features(snap)
        # spread = (101 - 99) / 100 = 0.02
        assert result["bid_ask_spread"] == pytest.approx(0.02, rel=1e-6)

    def test_balanced_book_zero_imbalance(self):
        snap = self._snapshot([(99.0, 5.0)], [(101.0, 5.0)])
        result = compute_orderbook_features(snap)
        assert result["book_imbalance"] == pytest.approx(0.0)

    def test_bid_heavy_positive_imbalance(self):
        snap = self._snapshot([(99.0, 8.0)], [(101.0, 2.0)])
        result = compute_orderbook_features(snap)
        assert result["book_imbalance"] > 0

    def test_ask_heavy_negative_imbalance(self):
        snap = self._snapshot([(99.0, 2.0)], [(101.0, 8.0)])
        result = compute_orderbook_features(snap)
        assert result["book_imbalance"] < 0


# ── Funding Features ─────────────────────────────────────────────────────────

class TestFundingFeatures:
    def _funding(self, rate: float) -> FundingRateData:
        return FundingRateData(
            symbol="BTCUSDT",
            funding_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            funding_rate=Decimal(str(rate)),
            mark_price=Decimal("50000"),
        )

    def test_none_returns_nones(self):
        result = compute_funding_features(None)
        assert all(v is None for v in result.values())

    def test_positive_rate_is_long_heavy(self):
        result = compute_funding_features(self._funding(0.001))
        assert result["funding_bias"] == "long_heavy"

    def test_negative_rate_is_short_heavy(self):
        result = compute_funding_features(self._funding(-0.001))
        assert result["funding_bias"] == "short_heavy"

    def test_near_zero_rate_is_neutral(self):
        result = compute_funding_features(self._funding(0.00001))
        assert result["funding_bias"] == "neutral"

    def test_annualized_calculation(self):
        result = compute_funding_features(self._funding(0.0001))
        # 0.0001 × 3 × 365 = 0.1095
        assert result["annualized_funding"] == pytest.approx(0.0001 * 3 * 365, rel=1e-6)


# ── Regime Classifier ─────────────────────────────────────────────────────────

class TestRegimeClassifier:
    def test_insufficient_data_returns_range_bound(self):
        result = classify_regime([100.0] * 5, None, None, None)
        assert result == Regime.RANGE_BOUND

    def test_flat_market_low_volatility(self):
        # Perfectly flat prices → near-zero rolling_std → Low Volatility
        prices = [100.0] * 60
        result = classify_regime(prices, rolling_std=0.000001, ema_fast=100.0, ema_slow=100.0)
        assert result == Regime.LOW_VOLATILITY

    def test_volatile_market_high_volatility(self):
        # Alternate between 100 and 110 → high rolling std
        prices = [100.0 + (i % 2) * 10.0 for i in range(60)]
        import statistics
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        std = statistics.stdev(returns)
        result = classify_regime(prices, rolling_std=std * 3, ema_fast=105.0, ema_slow=100.0)
        assert result == Regime.HIGH_VOLATILITY

    def test_trending_market(self):
        # Steady uptrend → EMA fast >> EMA slow → Trending
        prices = [100.0 + i * 0.5 for i in range(60)]
        import statistics
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        std = statistics.stdev(returns)
        # Use moderate rolling_std (not extreme)
        result = classify_regime(prices, rolling_std=std, ema_fast=130.0, ema_slow=110.0)
        assert result == Regime.TRENDING

    def test_regime_constants_exist(self):
        assert Regime.TRENDING == "trending"
        assert Regime.RANGE_BOUND == "range_bound"
        assert Regime.HIGH_VOLATILITY == "high_volatility"
        assert Regime.LOW_VOLATILITY == "low_volatility"


# ── Pipeline Integration ──────────────────────────────────────────────────────

class TestFeaturePipeline:
    def test_empty_candles_returns_none(self):
        pipeline = FeaturePipeline()
        result = pipeline.compute("BTCUSDT", [])
        assert result is None

    def test_insufficient_candles_returns_feature_set(self):
        # Even with few candles, pipeline returns a FeatureSet (features may be None)
        pipeline = FeaturePipeline()
        candles = _candles([100.0] * 5)
        result = pipeline.compute("BTCUSDT", candles)
        assert result is not None
        assert result.symbol == "BTCUSDT"

    def test_full_feature_set_with_enough_candles(self):
        pipeline = FeaturePipeline()
        candles = _candles([100.0 + i * 0.1 for i in range(60)])
        result = pipeline.compute("BTCUSDT", candles)
        assert result is not None
        assert result.zscore is not None
        assert result.ema_fast is not None
        assert result.ema_slow is not None
        assert result.atr is not None
        assert result.rolling_std is not None
        assert result.regime is not None

    def test_compute_batch_returns_one_per_candle(self):
        pipeline = FeaturePipeline()
        candles = _candles([100.0 + i * 0.1 for i in range(30)])
        results = pipeline.compute_batch("BTCUSDT", candles)
        assert len(results) == len(candles)

    def test_timestamp_is_candle_close_time(self):
        pipeline = FeaturePipeline()
        candles = _candles([100.0] * 25)
        result = pipeline.compute("BTCUSDT", candles)
        assert result.timestamp == candles[-1].close_time
