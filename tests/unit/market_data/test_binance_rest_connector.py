from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from maais.market_data.connectors.binance_rest import (
    PUBLIC_FAPI_BASE_URL,
    BinanceRestConnector,
)
from maais.market_data.events import FundingSettlementPayload, MarketEventKind
from tests.unit.market_data.test_binance_rest_contracts import (
    OBSERVED_AT,
    SERVER_MS,
    _exchange_info,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=PUBLIC_FAPI_BASE_URL,
        transport=httpx.MockTransport(handler),
    )


async def _no_sleep(_: float) -> None:
    return None


async def test_public_preflight_is_keyless_and_loads_advertised_weight_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "fapi.binance.com"
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.params
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER_MS})
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=_exchange_info())
        raise AssertionError(f"unexpected request: {request.url}")

    client = _client(handler)
    connector = BinanceRestConnector(
        client=client,
        observed_now=lambda: OBSERVED_AT,
        sleep=_no_sleep,
    )
    async with connector:
        preflight = await connector.preflight(("BTCUSDT", "ETHUSDT"))

        assert preflight.request_weight_limit_per_minute == 2400
        assert connector.request_weight_limit_per_minute == 2400
        assert connector.preflight_complete
    assert [request.url.path for request in requests] == [
        "/fapi/v1/time",
        "/fapi/v1/exchangeInfo",
    ]
    assert not client.is_closed
    await client.aclose()


async def test_depth_and_backfill_refuse_to_run_before_symbol_preflight() -> None:
    client = _client(lambda request: httpx.Response(500))
    async with BinanceRestConnector(
        client=client,
        observed_now=lambda: OBSERVED_AT,
        sleep=_no_sleep,
    ) as connector:
        with pytest.raises(RuntimeError, match="preflight"):
            await connector.get_depth_snapshot("BTCUSDT")
        with pytest.raises(RuntimeError, match="preflight"):
            await connector.get_closed_bar_events(
                "BTCUSDT",
                "1m",
                OBSERVED_AT - timedelta(minutes=1),
                OBSERVED_AT,
            )
    await client.aclose()


async def test_preflighted_depth_and_closed_bar_use_strict_public_contracts() -> None:
    counts: defaultdict[str, int] = defaultdict(int)

    def handler(request: httpx.Request) -> httpx.Response:
        counts[request.url.path] += 1
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER_MS})
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=_exchange_info())
        if request.url.path == "/fapi/v1/depth":
            assert dict(request.url.params) == {"symbol": "BTCUSDT", "limit": "1000"}
            return httpx.Response(
                200,
                json={
                    "lastUpdateId": 100,
                    "E": SERVER_MS - 5,
                    "T": SERVER_MS - 8,
                    "bids": [["100", "2"]],
                    "asks": [["101", "2"]],
                },
            )
        if request.url.path == "/fapi/v1/klines":
            assert request.url.params["startTime"] == str(SERVER_MS - 60_000)
            assert request.url.params["endTime"] == str(SERVER_MS - 1)
            return httpx.Response(
                200,
                json=[
                    [
                        SERVER_MS - 60_000,
                        "100",
                        "102",
                        "99",
                        "101",
                        "12.5",
                        SERVER_MS - 1,
                        "1260",
                        26,
                        "7",
                        "706",
                        "0",
                    ]
                ],
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = _client(handler)
    connector = BinanceRestConnector(
        client=client,
        observed_now=lambda: OBSERVED_AT,
        sleep=_no_sleep,
    )
    async with connector:
        await connector.preflight(("BTCUSDT", "ETHUSDT"))
        depth = await connector.get_depth_snapshot("BTCUSDT")
        bars = await connector.get_closed_bar_events(
            "BTCUSDT",
            "1m",
            datetime.fromtimestamp((SERVER_MS - 60_000) / 1000, tz=timezone.utc),
            datetime.fromtimestamp(SERVER_MS / 1000, tz=timezone.utc),
        )

    assert depth.last_update_id == 100
    assert len(bars) == 1 and bars[0].kind is MarketEventKind.CLOSED_BAR
    assert counts["/fapi/v1/depth"] == 1
    assert counts["/fapi/v1/klines"] == 1
    await client.aclose()


async def test_funding_history_paginates_inclusive_range_without_duplicates() -> None:
    calls = 0
    eight_hours_ms = 8 * 60 * 60 * 1000

    def funding(time_ms: int, rate_type: str = "Regular") -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "fundingTime": time_ms,
            "fundingRate": "0.0001",
            "markPrice": "100.5",
            "rateType": rate_type,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": SERVER_MS})
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=_exchange_info())
        if request.url.path != "/fapi/v1/fundingRate":
            raise AssertionError(f"unexpected request: {request.url}")
        calls += 1
        if calls == 1:
            assert request.url.params["startTime"] == str(SERVER_MS)
            return httpx.Response(
                200,
                json=[funding(SERVER_MS), funding(SERVER_MS + eight_hours_ms)],
            )
        assert request.url.params["startTime"] == str(SERVER_MS + eight_hours_ms + 1)
        return httpx.Response(
            200,
            json=[funding(SERVER_MS + 2 * eight_hours_ms, "Special")],
        )

    observed = datetime.fromtimestamp(
        (SERVER_MS + 3 * eight_hours_ms) / 1000,
        tz=timezone.utc,
    )
    client = _client(handler)
    connector = BinanceRestConnector(
        client=client,
        observed_now=lambda: observed,
        sleep=_no_sleep,
    )
    async with connector:
        await connector.preflight(("BTCUSDT", "ETHUSDT"))
        events = await connector.get_funding_events(
            "BTCUSDT",
            start_ms=SERVER_MS,
            end_ms=SERVER_MS + 2 * eight_hours_ms,
            page_limit=2,
        )

    assert calls == 2
    assert len(events) == 3
    assert len({event.event_id for event in events}) == 3
    assert isinstance(events[-1].payload, FundingSettlementPayload)
    assert events[-1].payload.rate_type == "Special"
    await client.aclose()
