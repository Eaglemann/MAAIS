import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from maais.market_data.connectors.binance_spot import (
    PUBLIC_BINANCE_SPOT_API_BASE_URL,
    BinanceSpotConnector,
    BinanceSpotContractError,
    parse_binance_spot_book_tickers,
    parse_binance_spot_exchange_info,
    parse_binance_spot_server_time,
)
from maais.market_data.events import ReferenceKind, ReferencePricePayload
from maais.market_data.frames import SourceObservation, TimestampBasis

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


def _book_ticker(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "bidPrice": "100.00000000",
        "bidQty": "2.00000000",
        "askPrice": "100.50000000",
        "askQty": "3.00000000",
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


def test_book_ticker_retains_exact_quote_and_explicit_observation_time_basis() -> None:
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings

    events = parse_binance_spot_book_tickers(
        [_book_ticker("BTCUSDT"), _book_ticker("ETHUSDT")],
        mappings=mappings,
        observed_at=OBSERVED_AT,
    )

    event = events[0]
    assert event.venue == "binance_spot"
    assert event.stream == "rest:/api/v3/ticker/bookTicker"
    assert event.sequence is None
    assert event.sequence_not_applicable_reason == "binance_spot_book_ticker_has_no_sequence"
    assert event.venue_event_at == OBSERVED_AT
    assert SourceObservation.from_event(event).timestamp_basis is TimestampBasis.LOCAL_OBSERVATION
    assert isinstance(event.payload, ReferencePricePayload)
    assert event.payload.reference_kind is ReferenceKind.PRIMARY_SPOT
    assert event.payload.price == Decimal("100.25")
    assert event.payload.source_bid == Decimal("100")
    assert event.payload.source_ask == Decimal("100.5")
    assert event.payload.source_published_at is None
    assert event.payload.source_quantity is None
    assert event.payload.source_side is None
    assert event.payload.source_event_id == (
        "100.00000000:2.00000000:100.50000000:3.00000000:1785672000100000"
    )


def test_repeated_rest_snapshot_has_distinct_observation_identity() -> None:
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings
    first = parse_binance_spot_book_tickers(
        [_book_ticker("BTCUSDT"), _book_ticker("ETHUSDT")],
        mappings=mappings,
        observed_at=OBSERVED_AT,
    )[0]
    second = parse_binance_spot_book_tickers(
        [_book_ticker("BTCUSDT"), _book_ticker("ETHUSDT")],
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
        "bidPrice",
        "bidQty",
        "askPrice",
        "askQty",
    ),
)
def test_book_ticker_has_no_missing_field_defaults(field: str) -> None:
    row = _book_ticker("BTCUSDT")
    row.pop(field)
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings

    with pytest.raises(BinanceSpotContractError, match=field):
        parse_binance_spot_book_tickers(
            [row, _book_ticker("ETHUSDT")],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )


def test_book_ticker_rejects_missing_duplicate_and_crossed_quotes() -> None:
    mappings = parse_binance_spot_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=parse_binance_spot_server_time({"serverTime": SERVER_MS}),
        observed_at=OBSERVED_AT,
    ).mappings

    with pytest.raises(BinanceSpotContractError, match="missing"):
        parse_binance_spot_book_tickers(
            [_book_ticker("BTCUSDT")],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(BinanceSpotContractError, match="duplicate"):
        parse_binance_spot_book_tickers(
            [_book_ticker("BTCUSDT"), _book_ticker("BTCUSDT")],
            mappings=mappings,
            observed_at=OBSERVED_AT,
        )
    crossed = _book_ticker("BTCUSDT")
    crossed["askPrice"] = crossed["bidPrice"]
    with pytest.raises(BinanceSpotContractError, match="crossed or locked"):
        parse_binance_spot_book_tickers(
            [crossed, _book_ticker("ETHUSDT")],
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
        if request.url.path == "/api/v3/ticker/bookTicker":
            assert json.loads(request.url.params["symbols"]) == ["BTCUSDT", "ETHUSDT"]
            assert request.url.params["symbolStatus"] == "TRADING"
            return httpx.Response(
                200,
                json=[_book_ticker("BTCUSDT"), _book_ticker("ETHUSDT")],
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
        "/api/v3/ticker/bookTicker",
    ]
    assert not client.is_closed
    await client.aclose()


async def test_spot_rest_retries_a_transient_transport_failure() -> None:
    ticker_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ticker_requests
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": SERVER_MS})
        if request.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(200, json=_exchange_info())
        if request.url.path == "/api/v3/ticker/bookTicker":
            ticker_requests += 1
            if ticker_requests == 1:
                raise httpx.ConnectError("connection reset", request=request)
            return httpx.Response(
                200,
                json=[_book_ticker("BTCUSDT"), _book_ticker("ETHUSDT")],
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
        await connector.preflight(("BTCUSDT", "ETHUSDT"))
        events = await connector.get_reference_events()

    assert [event.symbol for event in events] == ["BTCUSDT", "ETHUSDT"]
    assert ticker_requests == 2
    await client.aclose()
