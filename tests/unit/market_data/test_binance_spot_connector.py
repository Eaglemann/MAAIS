import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from maais.market_data.connectors.binance_spot import (
    PUBLIC_BINANCE_SPOT_API_BASE_URL,
    BinanceSpotConnector,
    BinanceSpotContractError,
    parse_binance_spot_exchange_info,
    parse_binance_spot_reference_tickers,
    parse_binance_spot_server_time,
)
from maais.market_data.events import ReferenceKind, ReferencePricePayload

OBSERVED_AT = datetime(2026, 8, 2, 12, 0, 0, 100_000, tzinfo=timezone.utc)
SERVER_MS = 1785672000000


def _exchange_info() -> dict[str, object]:
    return {
        "timezone": "UTC",
        "serverTime": SERVER_MS + 5,
        "rateLimits": [
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": 6000,
            },
            {
                "rateLimitType": "RAW_REQUESTS",
                "interval": "MINUTE",
                "intervalNum": 5,
                "limit": 300000,
            },
        ],
        "exchangeFilters": [],
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET"],
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "ETHUSDT",
                "status": "TRADING",
                "baseAsset": "ETH",
                "quoteAsset": "USDT",
                "orderTypes": ["LIMIT", "MARKET"],
                "isSpotTradingAllowed": True,
            },
        ],
    }


def _ticker(symbol: str, *, offset: int = 0) -> dict[str, object]:
    return {
        "symbol": symbol,
        "priceChange": "1.00000000",
        "priceChangePercent": "1.000",
        "weightedAvgPrice": "100.00000000",
        "prevClosePrice": "99.00000000",
        "lastPrice": "100.25000000",
        "lastQty": "0.50000000",
        "bidPrice": "100.00000000",
        "bidQty": "2.00000000",
        "askPrice": "100.50000000",
        "askQty": "3.00000000",
        "openPrice": "99.25000000",
        "highPrice": "102.00000000",
        "lowPrice": "98.00000000",
        "volume": "1000.00000000",
        "quoteVolume": "100000.00000000",
        "openTime": SERVER_MS - 86_400_000 + offset,
        "closeTime": SERVER_MS + offset,
        "firstId": 100 + offset,
        "lastId": 200 + offset,
        "count": 101,
    }


def test_spot_preflight_requires_every_explicit_trading_mapping() -> None:
    server_time = parse_binance_spot_server_time({"serverTime": SERVER_MS})

    result = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=server_time,
        observed_at=OBSERVED_AT,
    )

    assert result.request_weight_limit_per_minute == 6000
    assert [(item.primary_symbol, item.spot_symbol) for item in result.mappings] == [
        ("BTCUSDT", "BTCUSDT"),
        ("ETHUSDT", "ETHUSDT"),
    ]
    assert result.mappings[0].status == "TRADING"
    with pytest.raises(BinanceSpotContractError, match="missing"):
        parse_binance_spot_exchange_info(
            _exchange_info(),
            required_symbols=("BTCUSDT", "SOLUSDT"),
            server_time=server_time,
            observed_at=OBSERVED_AT,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("status", "HALT", "not TRADING"),
        ("quoteAsset", "USDC", "mapping differs"),
        ("isSpotTradingAllowed", False, "not enabled"),
    ),
)
def test_spot_preflight_fails_closed_on_mapping_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    raw = _exchange_info()
    symbols = raw["symbols"]
    assert isinstance(symbols, list) and isinstance(symbols[0], dict)
    symbols[0][field] = value

    with pytest.raises(BinanceSpotContractError, match=message):
        parse_binance_spot_exchange_info(
            raw,
            required_symbols=("BTCUSDT", "ETHUSDT"),
            server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
            observed_at=OBSERVED_AT,
        )


def test_reference_ticker_retains_exact_quote_and_venue_time() -> None:
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings

    events = parse_binance_spot_reference_tickers(
        [_ticker("BTCUSDT"), _ticker("ETHUSDT", offset=1)],
        mappings=mappings,
        observed_at=OBSERVED_AT,
    )

    event = events[0]
    assert event.venue == "binance_spot"
    assert event.stream == "rest:/api/v3/ticker/24hr"
    assert event.sequence is None
    assert event.sequence_not_applicable_reason == "binance_spot_ticker_has_no_book_sequence"
    assert event.venue_event_at == datetime.fromtimestamp(SERVER_MS / 1000, tz=timezone.utc)
    assert isinstance(event.payload, ReferencePricePayload)
    assert event.payload.reference_kind is ReferenceKind.PRIMARY_SPOT
    assert event.payload.price == Decimal("100.25")
    assert event.payload.source_bid == Decimal("100")
    assert event.payload.source_ask == Decimal("100.5")
    assert event.payload.source_published_at == event.venue_event_at
    assert event.payload.source_quantity is None
    assert event.payload.source_side is None
    assert event.payload.source_event_id == (
        "1785672000000:100:200:101:100.00000000:100.50000000:1785672000100000"
    )


def test_repeated_rest_snapshot_has_distinct_observation_identity() -> None:
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings
    first = parse_binance_spot_reference_tickers(
        [_ticker("BTCUSDT"), _ticker("ETHUSDT", offset=1)],
        mappings=mappings,
        observed_at=OBSERVED_AT,
    )[0]
    second = parse_binance_spot_reference_tickers(
        [_ticker("BTCUSDT"), _ticker("ETHUSDT", offset=1)],
        mappings=mappings,
        observed_at=OBSERVED_AT.replace(microsecond=200_000),
    )[0]

    assert first.event_id != second.event_id
    assert first.identity != second.identity
    assert first.content_hash != second.content_hash


@pytest.mark.parametrize(
    "field",
    (
        "symbol",
        "lastPrice",
        "bidPrice",
        "bidQty",
        "askPrice",
        "askQty",
        "openTime",
        "closeTime",
        "firstId",
        "lastId",
        "count",
    ),
)
def test_reference_ticker_has_no_missing_field_defaults(field: str) -> None:
    row = _ticker("BTCUSDT")
    row.pop(field)
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings

    with pytest.raises(BinanceSpotContractError, match=field):
        parse_binance_spot_reference_tickers(
            [row, _ticker("ETHUSDT", offset=1)],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )


def test_reference_ticker_rejects_missing_duplicate_and_crossed_quotes() -> None:
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings

    with pytest.raises(BinanceSpotContractError, match="missing"):
        parse_binance_spot_reference_tickers(
            [_ticker("BTCUSDT")],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(BinanceSpotContractError, match="duplicate"):
        parse_binance_spot_reference_tickers(
            [_ticker("BTCUSDT"), _ticker("BTCUSDT", offset=1)],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )
    crossed = _ticker("BTCUSDT")
    crossed["askPrice"] = crossed["bidPrice"]
    with pytest.raises(BinanceSpotContractError, match="crossed or locked"):
        parse_binance_spot_reference_tickers(
            [crossed, _ticker("ETHUSDT", offset=1)],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )


async def _no_sleep(_: float) -> None:
    return None


async def test_keyless_connector_gates_one_batch_poll_on_preflight() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.binance.com"
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.params
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": SERVER_MS})
        if request.url.path == "/api/v3/exchangeInfo":
            assert json.loads(request.url.params["symbols"]) == ["BTCUSDT", "ETHUSDT"]
            return httpx.Response(200, json=_exchange_info())
        if request.url.path == "/api/v3/ticker/24hr":
            assert json.loads(request.url.params["symbols"]) == ["BTCUSDT", "ETHUSDT"]
            assert request.url.params["type"] == "FULL"
            assert request.url.params["symbolStatus"] == "TRADING"
            return httpx.Response(
                200,
                json=[_ticker("BTCUSDT"), _ticker("ETHUSDT", offset=1)],
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(
        base_url=PUBLIC_BINANCE_SPOT_API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    connector = BinanceSpotConnector(
        client=client,
        observed_now=lambda: OBSERVED_AT,
        sleep=_no_sleep,
    )
    async with connector:
        with pytest.raises(RuntimeError, match="preflight"):
            await connector.get_reference_events()
        preflight = await connector.preflight(("BTCUSDT", "ETHUSDT"))
        events = await connector.get_reference_events()

    assert preflight.request_weight_limit_per_minute == 6000
    assert connector.request_weight_limit_per_minute == 6000
    assert [event.symbol for event in events] == ["BTCUSDT", "ETHUSDT"]
    assert [request.url.path for request in requests] == [
        "/api/v3/time",
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/24hr",
    ]
    assert not client.is_closed
    await client.aclose()
