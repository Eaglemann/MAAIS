"""Tests for market_data.aggregator — 1m → 5m/15m/1h OHLCV aggregation."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from maais.market_data.aggregator import aggregate_klines
from maais.market_data.schemas import KlineData


def _make_candle(
    minute: int,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: float = 10.0,
) -> KlineData:
    """Build a synthetic 1m candle at the given minute offset from midnight UTC."""
    hour, min_ = divmod(minute, 60)
    ot = datetime(2024, 1, 1, hour, min_, 0, tzinfo=timezone.utc)
    ct = datetime(2024, 1, 1, hour, min_, 59, tzinfo=timezone.utc)
    return KlineData(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=ot,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        close_time=ct,
        quote_volume=Decimal("1000.0"),
        trade_count=50,
        taker_buy_volume=Decimal("5.0"),
        taker_buy_quote_volume=Decimal("500.0"),
        is_closed=True,
    )


def _make_candles(minutes: list[int], **kwargs) -> list[KlineData]:
    return [_make_candle(m, **kwargs) for m in minutes]


class TestAggregator5m:
    def test_exact_window_produces_one_candle(self):
        candles = _make_candles(list(range(0, 5)))  # 00:00–00:04
        result = aggregate_klines(candles, "5m")
        assert len(result) == 1

    def test_two_full_windows(self):
        candles = _make_candles(list(range(0, 10)))
        result = aggregate_klines(candles, "5m")
        assert len(result) == 2

    def test_incomplete_window_dropped(self):
        candles = _make_candles(list(range(0, 7)))  # 5 + 2 incomplete
        result = aggregate_klines(candles, "5m")
        assert len(result) == 1

    def test_open_is_first_candle(self):
        candles = _make_candles(list(range(0, 5)), open_=200.0)
        candles[0] = _make_candle(0, open_=100.0)
        result = aggregate_klines(candles, "5m")
        assert result[0].open == Decimal("100.0")

    def test_close_is_last_candle(self):
        candles = _make_candles(list(range(0, 5)))
        candles[-1] = _make_candle(4, close=999.0)
        result = aggregate_klines(candles, "5m")
        assert result[0].close == Decimal("999.0")

    def test_high_is_max(self):
        candles = _make_candles(list(range(0, 5)))
        candles[2] = _make_candle(2, high=500.0)
        result = aggregate_klines(candles, "5m")
        assert result[0].high == Decimal("500.0")

    def test_low_is_min(self):
        candles = _make_candles(list(range(0, 5)))
        candles[1] = _make_candle(1, low=1.0)
        result = aggregate_klines(candles, "5m")
        assert result[0].low == Decimal("1.0")

    def test_volume_is_summed(self):
        candles = _make_candles(list(range(0, 5)), volume=2.0)
        result = aggregate_klines(candles, "5m")
        assert result[0].volume == Decimal("10.0")  # 5 × 2.0

    def test_trade_count_is_summed(self):
        candles = _make_candles(list(range(0, 5)))
        result = aggregate_klines(candles, "5m")
        assert result[0].trade_count == 250  # 5 × 50

    def test_timeframe_label(self):
        candles = _make_candles(list(range(0, 5)))
        result = aggregate_klines(candles, "5m")
        assert result[0].timeframe == "5m"

    def test_symbol_preserved(self):
        candles = _make_candles(list(range(0, 5)))
        result = aggregate_klines(candles, "5m")
        assert result[0].symbol == "BTCUSDT"


class TestAggregator1h:
    def test_one_hour_produces_one_candle(self):
        candles = _make_candles(list(range(0, 60)))
        result = aggregate_klines(candles, "1h")
        assert len(result) == 1

    def test_two_hours(self):
        candles = _make_candles(list(range(0, 120)))
        result = aggregate_klines(candles, "1h")
        assert len(result) == 2


class TestAggregatorEdgeCases:
    def test_empty_input(self):
        assert aggregate_klines([], "5m") == []

    def test_unclosed_candles_skipped(self):
        candles = _make_candles(list(range(0, 5)))
        candles[2].is_closed = False
        result = aggregate_klines(candles, "5m")
        # Only 4 closed candles → window of 5 is incomplete → dropped
        assert len(result) == 0

    def test_unknown_timeframe_raises(self):
        candles = _make_candles(list(range(0, 5)))
        with pytest.raises(ValueError, match="Unknown timeframe"):
            aggregate_klines(candles, "2m")
