"""Tests for market_data.integrity.validator — Rule 16 six checks."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from maais.market_data.integrity.validator import DataIntegrityValidator
from maais.market_data.schemas import KlineData


def _make_candles(
    count: int,
    start_minute: int = 0,
    timeframe: str = "1m",
    symbol: str = "BTCUSDT",
    close_prices: list[float] | None = None,
) -> list[KlineData]:
    """Create a contiguous series of candles."""
    interval = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[timeframe]
    base = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=start_minute)
    candles = []
    for i in range(count):
        ot = base + timedelta(minutes=i * interval)
        ct = ot + timedelta(minutes=interval, seconds=-1)
        close = Decimal(str(close_prices[i])) if close_prices else Decimal("100.0")
        candles.append(
            KlineData(
                symbol=symbol,
                timeframe=timeframe,
                open_time=ot,
                open=Decimal("100.0"),
                high=Decimal("101.0"),
                low=Decimal("99.0"),
                close=close,
                volume=Decimal("10.0"),
                close_time=ct,
                quote_volume=Decimal("1000.0"),
                trade_count=50,
                taker_buy_volume=Decimal("5.0"),
                taker_buy_quote_volume=Decimal("500.0"),
                is_closed=True,
            )
        )
    return candles


validator = DataIntegrityValidator()


class TestMissingData:
    def test_contiguous_passes(self):
        candles = _make_candles(10)
        result = validator.check_missing_data(candles, "1m")
        assert result.passed

    def test_gap_detected(self):
        candles = _make_candles(5)
        # Remove index 2 to create a gap
        del candles[2]
        result = validator.check_missing_data(candles, "1m")
        assert not result.passed
        assert "gap" in result.details

    def test_single_candle_passes(self):
        candles = _make_candles(1)
        result = validator.check_missing_data(candles, "1m")
        assert result.passed


class TestPriceOutliers:
    def test_flat_prices_pass(self):
        candles = _make_candles(20, close_prices=[100.0] * 20)
        result = validator.check_price_outliers(candles)
        assert result.passed

    def test_gradual_trend_passes(self):
        prices = [100.0 + i * 0.1 for i in range(20)]
        candles = _make_candles(20, close_prices=prices)
        result = validator.check_price_outliers(candles)
        assert result.passed

    def test_extreme_spike_detected(self):
        # With Z threshold=3.0, a 100% jump on a flat series is detectable.
        # Max achievable Z for a single outlier in n samples ≈ sqrt(n-1),
        # so we use a lower threshold for this test.
        prices = [100.0] * 19 + [200.0]  # 100% jump at the end
        candles = _make_candles(20, close_prices=prices)
        result = validator.check_price_outliers(candles, z_threshold=3.0)
        assert not result.passed

    def test_too_few_candles_passes(self):
        candles = _make_candles(2)
        result = validator.check_price_outliers(candles)
        assert result.passed


class TestCrossExchangeDivergence:
    def test_within_threshold_passes(self):
        result = validator.check_cross_exchange_divergence(
            "BTCUSDT",
            Decimal("50000"),
            Decimal("50500"),  # 1% divergence
        )
        assert result.passed

    def test_over_threshold_fails(self):
        result = validator.check_cross_exchange_divergence(
            "BTCUSDT",
            Decimal("50000"),
            Decimal("45000"),  # 10% divergence
        )
        assert not result.passed

    def test_zero_spot_price_skipped(self):
        result = validator.check_cross_exchange_divergence(
            "BTCUSDT", Decimal("50000"), Decimal("0")
        )
        assert result.passed


class TestTimestampSync:
    def test_aligned_timestamps_pass(self):
        candles = _make_candles(10)
        result = validator.check_timestamp_sync(candles, "1m")
        assert result.passed

    def test_misaligned_timestamp_fails(self):
        candles = _make_candles(10)
        # Shift one timestamp forward by 30 seconds
        candles[5].open_time = candles[5].open_time + timedelta(seconds=30)
        result = validator.check_timestamp_sync(candles, "1m")
        assert not result.passed


class TestApiOutage:
    def test_recent_data_passes(self):
        now = datetime.now(timezone.utc)
        result = validator.check_api_outage("BTCUSDT", now)
        assert result.passed

    def test_stale_data_fails(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=120)
        result = validator.check_api_outage("BTCUSDT", stale)
        assert not result.passed


class TestHistoricalCompleteness:
    def test_complete_dataset_passes(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 10, tzinfo=timezone.utc)  # 10 minutes
        candles = _make_candles(10, start_minute=0)
        result = validator.check_historical_completeness(candles, start, end, "1m")
        assert result.passed

    def test_empty_dataset_fails(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        result = validator.check_historical_completeness([], start, end, "1m")
        assert not result.passed

    def test_low_coverage_fails(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc)  # 60 min expected
        candles = _make_candles(30)  # only 50% coverage
        result = validator.check_historical_completeness(candles, start, end, "1m")
        assert not result.passed


class TestValidateAll:
    def test_all_checks_run(self):
        candles = _make_candles(10)
        results = validator.validate_candles(candles, "1m")
        assert len(results) == 3  # missing_data, price_outliers, timestamp_sync

    def test_all_passed_helper(self):
        candles = _make_candles(10)
        results = validator.validate_candles(candles, "1m")
        assert validator.all_passed(results)
