import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.market_data.connectors.binance_contracts import BinanceDepthSnapshot
from maais.market_data.connectors.binance_websocket import (
    PUBLIC_FSTREAM_BASE_URL,
    BinanceWebSocketConnector,
    ConnectorHalt,
    ConnectorState,
    _build_stream_url,
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


def _mark(event_ms: int) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@markPrice@1s",
            "data": {
                "e": "markPriceUpdate",
                "E": event_ms,
                "s": "BTCUSDT",
                "p": "100.25",
                "i": "100.10",
                "P": "100.20",
                "r": "0.0001",
                "T": event_ms + 8 * 60 * 60 * 1000,
            },
        }
    )


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def test_stream_url_uses_diff_depth_and_public_origin_without_credentials() -> None:
    url = _build_stream_url(("BTCUSDT", "ETHUSDT"))

    assert url.startswith(f"{PUBLIC_FSTREAM_BASE_URL}?streams=")
    assert "btcusdt@depth@500ms" in url
    assert "depth20" not in url
    assert "key=" not in url.lower()
    assert "signature=" not in url.lower()


async def test_start_ready_event_and_stop_retain_and_await_connector_task() -> None:
    socket = _Socket()
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: socket,
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=1,
    )

    await connector.start()
    task = connector.task
    assert task is not None and not task.done()
    assert connector.state is ConnectorState.READY
    socket.messages.put_nowait(_mark(1785672000000))

    event = await anext(connector.events())

    assert event.kind is MarketEventKind.MARK_FUNDING
    await connector.stop()
    assert socket.closed
    assert task.done()
    assert connector.state is ConnectorState.STOPPED


async def test_output_queue_saturation_halts_instead_of_dropping() -> None:
    socket = _Socket()
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: socket,
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        queue_size=1,
        ready_timeout=1,
    )
    await connector.start()
    socket.messages.put_nowait(_mark(1785672000000))
    socket.messages.put_nowait(_mark(1785672001000))

    await connector.wait_closed(timeout=1)

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "output_queue_saturated"
    events = connector.events()
    assert (await anext(events)).event_id.endswith("1785672000000")
    with pytest.raises(ConnectorHalt, match="output_queue_saturated"):
        await anext(events)


async def test_malformed_contract_halts_with_operator_visible_failure() -> None:
    socket = _Socket()
    connector = BinanceWebSocketConnector(
        ("BTCUSDT",),
        rest=_Rest(),
        connect=lambda _: socket,
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_no_sleep,
        jitter=lambda _low, _high: 0,
        ready_timeout=1,
    )
    await connector.start()
    socket.messages.put_nowait("{}")

    await connector.wait_closed(timeout=1)

    assert connector.state is ConnectorState.HALTED
    assert connector.failure is not None
    assert connector.failure.reason_code == "public_contract_violation"
    assert connector.failure.requires_operator_review


async def test_disconnect_enters_recovery_and_rebuilds_depth_before_ready() -> None:
    first = _Socket()
    second = _Socket()
    sockets = iter((first, second))
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
    await first.close()

    for _ in range(50):
        if connector.recovery_count == 1 and connector.state is ConnectorState.READY:
            break
        await asyncio.sleep(0)

    assert connector.recovery_count == 1
    assert connector.state is ConnectorState.READY
    assert connector.last_recovery_reason == "websocket_stream_ended"
    await connector.stop()
