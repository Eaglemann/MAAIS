"""Binance Futures WebSocket connector.

Subscribes to combined streams for all configured pairs:
  - <symbol>@kline_1m      — 1-minute candle updates
  - <symbol>@depth20@500ms — order book (top 20 levels, 500ms updates)
  - <symbol>@aggTrade      — aggregated trade stream
  - <symbol>@markPrice@1s  — mark price + funding rate (every second)

Architecture:
  Messages are placed onto an asyncio.Queue and consumed by the caller.
  This decouples ingestion speed from processing speed.

Reconnection:
  Exponential backoff starting at 1s, capped at 60s.
  Resets to 1s on successful connection held for > 30s.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from decimal import Decimal

import websockets
from websockets.exceptions import ConnectionClosed

from maais.core.logging import get_logger
from maais.market_data.schemas import FundingRateData, KlineData, OrderBookSnapshot, TradeData

logger = get_logger(__name__)

_WS_BASE = "wss://fstream.binance.com/stream"
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0
_STABLE_CONNECTION_SECONDS = 30.0


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _build_stream_url(symbols: list[str]) -> str:
    streams = []
    for sym in symbols:
        s = sym.lower()
        streams += [
            f"{s}@kline_1m",
            f"{s}@depth20@500ms",
            f"{s}@aggTrade",
            f"{s}@markPrice@1s",
        ]
    return f"{_WS_BASE}?streams=" + "/".join(streams)


def _parse_kline_event(data: dict) -> KlineData | None:
    k = data.get("k")
    if not k:
        return None
    return KlineData(
        symbol=k["s"],
        timeframe=k["i"],
        open_time=_ms_to_dt(k["t"]),
        open=Decimal(k["o"]),
        high=Decimal(k["h"]),
        low=Decimal(k["l"]),
        close=Decimal(k["c"]),
        volume=Decimal(k["v"]),
        close_time=_ms_to_dt(k["T"]),
        quote_volume=Decimal(k["q"]),
        trade_count=int(k["n"]),
        taker_buy_volume=Decimal(k["V"]),
        taker_buy_quote_volume=Decimal(k["Q"]),
        is_closed=bool(k["x"]),
    )


def _parse_depth_event(data: dict) -> OrderBookSnapshot | None:
    symbol = data.get("s")
    if not symbol:
        return None
    return OrderBookSnapshot(
        symbol=symbol,
        timestamp=_ms_to_dt(data.get("T", 0) or int(datetime.now(timezone.utc).timestamp() * 1000)),
        bids=[(Decimal(p), Decimal(q)) for p, q in data.get("b", [])],
        asks=[(Decimal(p), Decimal(q)) for p, q in data.get("a", [])],
        last_update_id=data.get("lastUpdateId", 0),
    )


def _parse_agg_trade_event(data: dict) -> TradeData | None:
    return TradeData(
        symbol=data["s"],
        timestamp=_ms_to_dt(data["T"]),
        price=Decimal(data["p"]),
        quantity=Decimal(data["q"]),
        is_buyer_maker=bool(data["m"]),
        trade_id=int(data["l"]),   # last trade ID in the aggregate
    )


def _parse_mark_price_event(data: dict) -> FundingRateData | None:
    fr = data.get("r")
    if fr is None:
        return None
    return FundingRateData(
        symbol=data["s"],
        funding_time=_ms_to_dt(int(data.get("T", 0))),
        funding_rate=Decimal(str(fr)),
        mark_price=Decimal(str(data.get("p", "0"))),
    )


# Union type for all possible parsed events
MarketEvent = KlineData | OrderBookSnapshot | TradeData | FundingRateData


class BinanceWebSocketConnector:
    """Connects to Binance Futures combined streams and emits parsed events."""

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols
        self._url = _build_stream_url(symbols)
        self._queue: asyncio.Queue[MarketEvent] = asyncio.Queue(maxsize=10_000)
        self._running = False

    def _parse_message(self, raw: str) -> MarketEvent | None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None

        data = msg.get("data", msg)
        event_type = data.get("e", "")

        if event_type == "kline":
            return _parse_kline_event(data)
        elif event_type == "depthUpdate":
            return _parse_depth_event(data)
        elif event_type == "aggTrade":
            return _parse_agg_trade_event(data)
        elif event_type == "markPriceUpdate":
            return _parse_mark_price_event(data)

        return None

    async def _run_connection(self) -> None:
        backoff = _BACKOFF_BASE
        while self._running:
            try:
                connect_time = asyncio.get_event_loop().time()
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("websocket_connected", symbols=len(self._symbols))
                    backoff = _BACKOFF_BASE   # reset on successful connect
                    async for raw in ws:
                        if not self._running:
                            return
                        event = self._parse_message(raw)
                        if event is not None:
                            try:
                                self._queue.put_nowait(event)
                            except asyncio.QueueFull:
                                logger.warning("event_queue_full_dropping_event")
                        # If connection has been stable, reset backoff
                        if asyncio.get_event_loop().time() - connect_time > _STABLE_CONNECTION_SECONDS:
                            backoff = _BACKOFF_BASE

            except ConnectionClosed as exc:
                logger.warning("websocket_disconnected", code=exc.code, reason=exc.reason)
            except Exception as exc:
                logger.error("websocket_error", error=str(exc))

            if self._running:
                logger.info("websocket_reconnecting", backoff_seconds=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._run_connection(), name="ws_connector")

    async def stop(self) -> None:
        self._running = False

    async def events(self) -> AsyncGenerator[MarketEvent, None]:
        """Async generator — yields parsed market events as they arrive."""
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue
