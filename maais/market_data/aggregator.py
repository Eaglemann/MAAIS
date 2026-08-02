"""Kline aggregator: 1m OHLCV → 5m / 15m / 1h.

Standard OHLCV aggregation rules:
  open  = first candle's open
  high  = max of all highs
  low   = min of all lows
  close = last candle's close
  volume = sum of all volumes
  quote_volume = sum
  trade_count = sum
  taker_buy_volume = sum
  taker_buy_quote_volume = sum
  open_time = first candle's open_time
  close_time = last candle's close_time
"""

from datetime import timezone
from decimal import Decimal

from maais.config.constants import TIMEFRAME_MINUTES
from maais.market_data.schemas import KlineData


def aggregate_klines(candles_1m: list[KlineData], target_timeframe: str) -> list[KlineData]:
    """Aggregate a list of 1m candles into the target timeframe.

    Args:
        candles_1m: Sorted list of closed 1m candles (ascending by open_time).
        target_timeframe: One of "5m", "15m", "1h".

    Returns:
        List of aggregated candles. Incomplete final windows are dropped.
    """
    if target_timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(
            f"Unknown timeframe: {target_timeframe!r}. Valid: {list(TIMEFRAME_MINUTES)}"
        )

    window_size = TIMEFRAME_MINUTES[target_timeframe]  # minutes per aggregated candle
    if window_size <= 1:
        return list(candles_1m)  # nothing to aggregate

    if not candles_1m:
        return []

    symbol = candles_1m[0].symbol
    result: list[KlineData] = []
    window: list[KlineData] = []

    def _window_start_minute(candle: KlineData) -> int:
        """Return the minute-of-day aligned to the window boundary."""
        dt = candle.open_time.astimezone(timezone.utc)
        total_minutes = dt.hour * 60 + dt.minute
        return (total_minutes // window_size) * window_size

    current_window_key: int | None = None

    for candle in candles_1m:
        if not candle.is_closed:
            continue  # skip live/unclosed candles
        window_key = _window_start_minute(candle)
        if current_window_key is None:
            current_window_key = window_key

        if window_key != current_window_key:
            # Flush completed window
            if len(window) == window_size:
                result.append(_merge(window, target_timeframe, symbol))
            window = [candle]
            current_window_key = window_key
        else:
            window.append(candle)

    # Flush last window only if complete
    if len(window) == window_size:
        result.append(_merge(window, target_timeframe, symbol))

    return result


def _merge(window: list[KlineData], timeframe: str, symbol: str) -> KlineData:
    return KlineData(
        symbol=symbol,
        timeframe=timeframe,
        open_time=window[0].open_time,
        open=window[0].open,
        high=max(c.high for c in window),
        low=min(c.low for c in window),
        close=window[-1].close,
        volume=sum((c.volume for c in window), Decimal(0)),
        close_time=window[-1].close_time,
        quote_volume=sum((c.quote_volume for c in window), Decimal(0)),
        trade_count=sum(c.trade_count for c in window),
        taker_buy_volume=sum((c.taker_buy_volume for c in window), Decimal(0)),
        taker_buy_quote_volume=sum((c.taker_buy_quote_volume for c in window), Decimal(0)),
        is_closed=True,
    )
