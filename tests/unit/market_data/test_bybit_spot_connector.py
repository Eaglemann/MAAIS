from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from maais.market_data.connectors.bybit_spot import (
    PUBLIC_BYBIT_API_BASE_URL,
    BybitContractError,
    BybitSpotConnector,
    parse_bybit_instruments,
    parse_bybit_reference_book,
    parse_bybit_server_time,
)
from maais.market_data.events import ReferenceKind, ReferencePricePayload

OBSERVED_AT = datetime(2026, 8, 2, 12, 0, 0, 100_000, tzinfo=timezone.utc)
SERVER_MS = 1785672000000


def _envelope(result: object, *, time_ms: int = SERVER_MS) -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": result,
        "retExtInfo": {},
        "time": time_ms,
    }


def _instruments() -> dict[str, object]:
    return _envelope(
        {
            "category": "spot",
            "list": [
                {
                    "symbolId": 1,
                    "symbol": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "innovation": "0",
                    "status": "Trading",
                    "marginTrading": "utaOnly",
                    "stTag": "0",
                    "lotSizeFilter": {},
                    "priceFilter": {},
                    "riskParameters": {},
                    "symbolType": "",
                },
                {
                    "symbolId": 2,
                    "symbol": "ETHUSDT",
                    "baseCoin": "ETH",
                    "quoteCoin": "USDT",
                    "innovation": "0",
                    "status": "Trading",
                    "marginTrading": "utaOnly",
                    "stTag": "0",
                    "lotSizeFilter": {},
                    "priceFilter": {},
                    "riskParameters": {},
                    "symbolType": "",
                },
            ],
        }
    )


def _book() -> dict[str, object]:
    return _envelope(
        {
            "s": "BTCUSDT",
            "b": [["100", "2"]],
            "a": [["101", "3"]],
            "ts": SERVER_MS - 3,
            "u": 1000,
            "seq": 2000,
            "cts": SERVER_MS - 5,
        }
    )


def test_bybit_preflight_requires_every_explicit_trading_spot_mapping() -> None:
    server_time = parse_bybit_server_time(
        _envelope({"timeSecond": str(SERVER_MS // 1000), "timeNano": str(SERVER_MS * 1_000_000)})
    )

    mappings = parse_bybit_instruments(
        _instruments(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=server_time,
        observed_at=OBSERVED_AT,
    )

    assert [(item.primary_symbol, item.bybit_symbol) for item in mappings] == [
        ("BTCUSDT", "BTCUSDT"),
        ("ETHUSDT", "ETHUSDT"),
    ]
    with pytest.raises(BybitContractError, match="missing"):
        parse_bybit_instruments(
            _instruments(),
            required_symbols=("BTCUSDT", "SOLUSDT"),
            server_time=server_time,
            observed_at=OBSERVED_AT,
        )


def test_reference_book_retains_exact_engine_time_sequence_and_executable_prices() -> None:
    event = parse_bybit_reference_book(
        _book(),
        primary_symbol="BTCUSDT",
        bybit_symbol="BTCUSDT",
        observed_at=OBSERVED_AT,
    )

    assert event.venue == "bybit_spot"
    assert event.venue_event_at == datetime.fromtimestamp(
        (SERVER_MS - 5) / 1000,
        tz=timezone.utc,
    )
    assert event.sequence == 2000
    assert isinstance(event.payload, ReferencePricePayload)
    assert event.payload.reference_kind is ReferenceKind.SECONDARY_VENUE
    assert event.payload.price == Decimal("100.5")
    assert event.payload.source_event_id == "1000:2000"
    assert event.payload.source_quantity is None
    assert event.payload.source_side is None
    assert event.payload.source_bid == Decimal("100")
    assert event.payload.source_ask == Decimal("101")
    assert event.payload.source_published_at == datetime.fromtimestamp(
        (SERVER_MS - 3) / 1000,
        tz=timezone.utc,
    )


def test_repeated_rest_book_has_distinct_observation_identity() -> None:
    first = parse_bybit_reference_book(
        _book(),
        primary_symbol="BTCUSDT",
        bybit_symbol="BTCUSDT",
        observed_at=OBSERVED_AT,
    )
    second = parse_bybit_reference_book(
        _book(),
        primary_symbol="BTCUSDT",
        bybit_symbol="BTCUSDT",
        observed_at=OBSERVED_AT.replace(microsecond=200_000),
    )

    assert first.event_id != second.event_id
    assert first.identity != second.identity
    assert first.content_hash != second.content_hash


@pytest.mark.parametrize("field", ("s", "b", "a", "ts", "u", "seq", "cts"))
def test_reference_book_has_no_missing_field_defaults(field: str) -> None:
    raw = _book()
    result = raw["result"]
    assert isinstance(result, dict)
    result.pop(field)

    with pytest.raises(BybitContractError, match=field):
        parse_bybit_reference_book(
            raw,
            primary_symbol="BTCUSDT",
            bybit_symbol="BTCUSDT",
            observed_at=OBSERVED_AT,
        )


async def _no_sleep(_: float) -> None:
    return None


async def test_keyless_connector_gates_reference_polling_on_preflight() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.bybit.com"
        assert "x-bapi-api-key" not in request.headers
        assert "x-bapi-sign" not in request.headers
        if request.url.path == "/v5/market/time":
            return httpx.Response(
                200,
                json=_envelope(
                    {
                        "timeSecond": str(SERVER_MS // 1000),
                        "timeNano": str(SERVER_MS * 1_000_000),
                    }
                ),
            )
        if request.url.path == "/v5/market/instruments-info":
            return httpx.Response(200, json=_instruments())
        if request.url.path == "/v5/market/orderbook":
            assert dict(request.url.params) == {
                "category": "spot",
                "symbol": "BTCUSDT",
                "limit": "1",
            }
            return httpx.Response(200, json=_book())
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(
        base_url=PUBLIC_BYBIT_API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    connector = BybitSpotConnector(
        client=client,
        observed_now=lambda: OBSERVED_AT,
        sleep=_no_sleep,
    )
    async with connector:
        with pytest.raises(RuntimeError, match="preflight"):
            await connector.get_reference_event("BTCUSDT")
        mappings = await connector.preflight(("BTCUSDT", "ETHUSDT"))
        event = await connector.get_reference_event("BTCUSDT")

    assert len(mappings) == 2
    assert isinstance(event.payload, ReferencePricePayload)
    assert event.payload.price == Decimal("100.5")
    assert [request.url.path for request in requests] == [
        "/v5/market/time",
        "/v5/market/instruments-info",
        "/v5/market/orderbook",
    ]
    assert not client.is_closed
    await client.aclose()
