import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.market_data.connectors.binance_contracts import BinanceDepthSnapshot
from maais.market_data.connectors.binance_websocket import (
    MARKET_FSTREAM_BASE_URL,
    PUBLIC_FSTREAM_BASE_URL,
    BinanceWebSocketConnector,
    ConnectorHalt,
    ConnectorState,
    _build_market_stream_url,
    _build_public_stream_url,
)
from maais.market_data.events import MarketEventKind

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
_END = object()


class _Rest:
    preflight_complete = True

    def __init__(self) -> None:
        self.symbols = {"BTCUSDT"}

    @property
    def preflight_result(self):
        class Result:
            exchange_filters = (type("Filter", (), {"symbol": "BTCUSDT"})(),)

        return Result()

    async def get_depth_snapshot(self, symbol: str) -> BinanceDepthSnapshot:
        assert symbol in self.symbols
        return BinanceDepthSnapshot(
            symbol=symbol,
            last_update_id=100,
            published_at=NOW,
            venue_event_at=NOW,
            observed_at=NOW + timedelta(milliseconds=1),
            bids=((Decimal("100"), Decimal("5")),),
            asks=((Decimal("101"), Decimal("5")),),
        )


class _Socket:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[object] = asyncio.Queue()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.messages.get()
        if value is _END:
            raise StopAsyncIteration
        assert isinstance(value, str)
        return value

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.messages.put_nowait(_END)


def _mark(event_ms: int, symbol: str = "BTCUSDT") -> str:
    stream_symbol = symbol.lower()
    return json.dumps(
        {
            "stream": f"{stream_symbol}@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "E": event_ms,
                "s": symbol,
                "p": "100.25",
                "i": "100.10",
                "P": "100.20",
                "r": "0.0001",
                "T": event_ms + 8 * 60 * 60 * 1000,
            },
        }
    )


def _depth(
    event_ms: int,
    *,
    first: int = 101,
    final: int = 102,
    previous: int = 100,
    symbol: str = "BTCUSDT",
) -> str:
    stream_symbol = symbol.lower()
    return json.dumps(
        {
            "stream": f"{stream_symbol}@depth@500ms",
            "data": {
                "e": "depthUpdate",
                "E": event_ms,
                "T": event_ms - 1,
                "s": symbol,
                "U": first,
                "u": final,
                "pu": previous,
                "b": [["100", "4"]],
                "a": [["101", "4"]],
            },
        }
    )


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def test_stream_urls_separate_public_depth_from_market_events_without_credentials() -> None:
    public_url = _build_public_stream_url(("BTCUSDT", "ETHUSDT"))
    market_url = _build_market_stream_url(("BTCUSDT", "ETHUSDT"))

    assert public_url.startswith(f"{PUBLIC_FSTREAM_BASE_URL}?streams=")
    assert "btcusdt@depth@500ms" in public_url
    assert "kline" not in public_url
    assert "markPrice" not in public_url
    assert market_url.startswith(f"{MARKET_FSTREAM_BASE_URL}?streams=")
    assert "btcusdt@kline_1m" in market_url
    assert "btcusdt@markPrice@1s" in market_url
    assert "depth" not in market_url
    for url in (public_url, market_url):
        assert "depth20" not in url
        assert "aggTrade" not in url
        assert "key=" not in url.lower()
        assert "signature=" not in url.lower()


async def test_start_ready_event_and_stop_retain_and_await_connector_task() -> None:
    public_socket = _Socket()
    market_socket = _Socket()
    public_socket.messages.put_nowait(_depth(1785672000000))
    market_socket.messages.put_nowait(_mark(1785672000000))
    sockets = iter((public_socket, market_socket))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=1,
    )

    await connector.start()
    task = connector.task
    assert task is not None and not task.done()
    assert connector.state is ConnectorState.READY

    events = connector.events()
    observed = {await anext(events), await anext(events)}

    assert {event.kind for event in observed} == {
        MarketEventKind.MARK_FUNDING,
        MarketEventKind.ORDER_BOOK,
    }
    await connector.stop()
    assert public_socket.closed
    assert market_socket.closed
    assert task.done()
    assert connector.state is ConnectorState.STOPPED


async def test_startup_does_not_claim_ready_before_each_order_book_is_published() -> None:
    public_socket = _Socket()
    market_socket = _Socket()
    market_socket.messages.put_nowait(_mark(1785672000000))
    sockets = iter((public_socket, market_socket))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=0.01,
    )

    with pytest.raises(ConnectorHalt, match="ready_timeout"):
        await connector.start()

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "ready_timeout"


async def test_output_queue_saturation_halts_instead_of_dropping() -> None:
    public_socket = _Socket()
    market_socket = _Socket()
    public_socket.messages.put_nowait(_depth(1785672000000))
    market_socket.messages.put_nowait(_mark(1785672000000))
    sockets = iter((public_socket, market_socket))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        queue_size=2,
        ready_timeout=1,
    )
    await connector.start()
    market_socket.messages.put_nowait(_mark(1785672001000))

    await connector.wait_closed(timeout=1)

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "output_queue_saturated"
    events = connector.events()
    retained = (await anext(events), await anext(events))
    assert {event.kind for event in retained} == {
        MarketEventKind.MARK_FUNDING,
        MarketEventKind.ORDER_BOOK,
    }
    with pytest.raises(ConnectorHalt, match="output_queue_saturated"):
        await anext(events)


async def test_malformed_contract_halts_with_operator_visible_failure() -> None:
    public_socket = _Socket()
    market_socket = _Socket()
    public_socket.messages.put_nowait(_depth(1785672000000))
    market_socket.messages.put_nowait(_mark(1785672000000))
    sockets = iter((public_socket, market_socket))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=1,
    )
    await connector.start()
    public_socket.messages.put_nowait("{}")

    await connector.wait_closed(timeout=1)

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "public_contract_violation"
    assert connector.failure.requires_operator_review


async def test_disconnect_enters_recovery_and_rebuilds_depth_before_ready() -> None:
    first_public = _Socket()
    first_market = _Socket()
    second_public = _Socket()
    second_market = _Socket()
    first_public.messages.put_nowait(_depth(1785672000000))
    first_market.messages.put_nowait(_mark(1785672000000))
    second_public.messages.put_nowait(_depth(1785672001000))
    second_market.messages.put_nowait(_mark(1785672001000))
    sockets = iter((first_public, first_market, second_public, second_market))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=1,
    )
    await connector.start()
    await first_public.close()

    for _ in range(50):
        if connector.recovery_count == 1 and connector.state is ConnectorState.READY:
            break
        await asyncio.sleep(0)

    assert connector.recovery_count == 1
    assert connector.state is ConnectorState.READY
    assert connector.last_recovery_reason == "websocket_stream_ended"
    await connector.stop()


async def test_startup_requires_mark_coverage_for_every_symbol() -> None:
    public_socket = _Socket()
    market_socket = _Socket()
    sockets = iter((public_socket, market_socket))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=0.01,
    )

    with pytest.raises(ConnectorHalt, match="ready_timeout"):
        await connector.start()

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "ready_timeout"


async def test_unconfigured_stream_symbol_halts_as_contract_violation() -> None:
    public_socket = _Socket()
    market_socket = _Socket()
    public_socket.messages.put_nowait(_depth(1785672000000))
    market_socket.messages.put_nowait(_mark(1785672000000))
    sockets = iter((public_socket, market_socket))
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: next(sockets),
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=1,
    )
    await connector.start()
    market_socket.messages.put_nowait(_mark(1785672001000, "ETHUSDT"))

    await connector.wait_closed(timeout=1)

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "public_contract_violation"
