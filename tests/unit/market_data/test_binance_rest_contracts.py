from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.domain.enums import PaperOrderType
from maais.market_data.connectors.binance_contracts import BinanceContractError
from maais.market_data.connectors.binance_rest_contracts import (
    parse_closed_bar_events,
    parse_depth_snapshot,
    parse_exchange_info,
    parse_funding_events,
    parse_server_time,
)
from maais.market_data.events import (
    ClosedBarPayload,
    FundingSettlementPayload,
    MarketEventKind,
    SymbolStatePayload,
    VenueClockPayload,
)
from maais.market_data.frames import SourceObservation, TimestampBasis

OBSERVED_AT = datetime(2026, 8, 2, 12, 0, 0, 100_000, tzinfo=timezone.utc)
SERVER_MS = 1785672000000


def _symbol(symbol: str, *, status: str = "TRADING") -> dict[str, object]:
    return {
        "symbol": symbol,
        "pair": symbol,
        "contractType": "PERPETUAL",
        "deliveryDate": 4133404800000,
        "onboardDate": 1569398400000,
        "status": status,
        "maintMarginPercent": "2.5000",
        "requiredMarginPercent": "5.0000",
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "baseAssetPrecision": 8,
        "quotePrecision": 8,
        "underlyingType": "COIN",
        "underlyingSubType": [],
        "settlePlan": 0,
        "triggerProtect": "0.0500",
        "liquidationFee": "0.012500",
        "marketTakeBound": "0.05",
        "maxMoveOrderLimit": 10000,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.10",
                "maxPrice": "1000000",
                "tickSize": "0.10",
            },
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            },
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "100", "stepSize": "0"},
            {"filterType": "MAX_NUM_ORDERS", "limit": 200},
            {"filterType": "MAX_NUM_ALGO_ORDERS", "limit": 10},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
            {
                "filterType": "PERCENT_PRICE",
                "multiplierUp": "1.0500",
                "multiplierDown": "0.9500",
                "multiplierDecimal": "4",
            },
            {"filterType": "POSITION_RISK_CONTROL", "positionControlSide": "NONE"},
        ],
        "orderTypes": [
            "LIMIT",
            "MARKET",
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
            "TRAILING_STOP_MARKET",
        ],
        "timeInForce": ["GTC", "IOC", "FOK", "GTX", "GTD"],
        "permissionSets": ["GRID", "COPY", "RPI"],
    }


def _exchange_info() -> dict[str, object]:
    return {
        "exchangeFilters": [],
        "rateLimits": [
            {
                "rateLimitType": "REQUEST_WEIGHT",
                "interval": "MINUTE",
                "intervalNum": 1,
                "limit": 2400,
            },
            {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200},
        ],
        "serverTime": 0,
        "assets": [],
        "symbols": [_symbol("BTCUSDT"), _symbol("ETHUSDT")],
        "timezone": "UTC",
    }


def test_server_clock_and_exchange_info_preflight_are_exact_and_dynamic() -> None:
    clock = parse_server_time({"serverTime": SERVER_MS}, observed_at=OBSERVED_AT)
    assert clock.kind is MarketEventKind.VENUE_CLOCK
    assert isinstance(clock.payload, VenueClockPayload)

    preflight = parse_exchange_info(
        _exchange_info(),
        required_symbols=("BTCUSDT", "ETHUSDT"),
        server_time=clock.payload.server_time,
        server_observed_at=OBSERVED_AT,
        observed_at=OBSERVED_AT + timedelta(milliseconds=1308),
    )

    assert preflight.request_weight_limit_per_minute == 2400
    assert [item.symbol for item in preflight.exchange_filters] == ["BTCUSDT", "ETHUSDT"]
    assert preflight.exchange_filters[0].supported_order_types == (
        PaperOrderType.LIMIT,
        PaperOrderType.MARKET,
    )
    assert preflight.exchange_filters[0].price_tick == Decimal("0.10")
    assert preflight.exchange_filters[0].minimum_notional == Decimal("5")
    assert all(event.kind is MarketEventKind.SYMBOL_STATE for event in preflight.symbol_states)
    assert isinstance(preflight.symbol_states[0].payload, SymbolStatePayload)
    assert preflight.symbol_states[0].payload.status == "TRADING"
    assert all(event.observed_at == OBSERVED_AT for event in preflight.venue_clocks)
    assert all(
        event.observed_at == OBSERVED_AT + timedelta(milliseconds=1308)
        for event in preflight.symbol_states
    )
    assert all(event.venue_event_at == event.observed_at for event in preflight.symbol_states)
    assert (
        SourceObservation.from_event(preflight.symbol_states[0]).timestamp_basis
        is TimestampBasis.LOCAL_OBSERVATION
    )


def test_exchange_info_rejects_missing_duplicate_or_nonperpetual_required_symbol() -> None:
    with pytest.raises(BinanceContractError, match="missing required symbols"):
        parse_exchange_info(
            _exchange_info(),
            required_symbols=("BTCUSDT", "SOLUSDT"),
            server_time=datetime.fromtimestamp(SERVER_MS / 1000, tz=timezone.utc),
            server_observed_at=OBSERVED_AT,
            observed_at=OBSERVED_AT,
        )
    duplicate = _exchange_info()
    assert isinstance(duplicate["symbols"], list)
    duplicate["symbols"].append(_symbol("BTCUSDT"))
    with pytest.raises(BinanceContractError, match="duplicate"):
        parse_exchange_info(
            duplicate,
            required_symbols=("BTCUSDT",),
            server_time=datetime.fromtimestamp(SERVER_MS / 1000, tz=timezone.utc),
            server_observed_at=OBSERVED_AT,
            observed_at=OBSERVED_AT,
        )
    inverse = _exchange_info()
    assert isinstance(inverse["symbols"], list)
    inverse["symbols"][0]["contractType"] = "CURRENT_QUARTER"  # type: ignore[index]
    with pytest.raises(BinanceContractError, match="perpetual"):
        parse_exchange_info(
            inverse,
            required_symbols=("BTCUSDT",),
            server_time=datetime.fromtimestamp(SERVER_MS / 1000, tz=timezone.utc),
            server_observed_at=OBSERVED_AT,
            observed_at=OBSERVED_AT,
        )


def test_depth_snapshot_requires_official_timestamp_update_id_and_levels() -> None:
    snapshot = parse_depth_snapshot(
        {
            "lastUpdateId": 1027024,
            "E": SERVER_MS - 5,
            "T": SERVER_MS - 8,
            "bids": [["100", "2"], ["99", "3"]],
            "asks": [["101", "2"], ["102", "3"]],
        },
        symbol="BTCUSDT",
        observed_at=OBSERVED_AT,
    )

    assert snapshot.last_update_id == 1027024
    assert snapshot.venue_event_at == datetime.fromtimestamp(
        (SERVER_MS - 8) / 1000, tz=timezone.utc
    )
    assert snapshot.bids[0] == (Decimal("100"), Decimal("2"))

    for field in ("lastUpdateId", "E", "T", "bids", "asks"):
        invalid = {
            "lastUpdateId": 1027024,
            "E": SERVER_MS - 5,
            "T": SERVER_MS - 8,
            "bids": [["100", "2"]],
            "asks": [["101", "2"]],
        }
        invalid.pop(field)
        with pytest.raises(BinanceContractError, match=field):
            parse_depth_snapshot(invalid, symbol="BTCUSDT", observed_at=OBSERVED_AT)


def test_rest_kline_backfill_has_same_canonical_bar_identity_as_live_stream() -> None:
    events = parse_closed_bar_events(
        [
            [
                1785671940000,
                "100",
                "102",
                "99",
                "101",
                "12.5",
                1785671999999,
                "1260",
                26,
                "7",
                "706",
                "0",
            ]
        ],
        symbol="BTCUSDT",
        interval="1m",
        observed_at=OBSERVED_AT,
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_id == "binance_usdm:btcusdt@kline_1m:BTCUSDT:bar:1785671940000"
    assert event.sequence == 1785671940000 // 60_000
    assert isinstance(event.payload, ClosedBarPayload)
    assert event.payload.bar_close_at == datetime.fromtimestamp(SERVER_MS / 1000, tz=timezone.utc)


def test_funding_history_requires_mark_price_rate_type_and_ascending_time() -> None:
    raw = [
        {
            "symbol": "BTCUSDT",
            "fundingTime": SERVER_MS,
            "fundingRate": "0.0001",
            "markPrice": "100.5",
            "rateType": "Regular",
        },
        {
            "symbol": "BTCUSDT",
            "fundingTime": SERVER_MS + 8 * 60 * 60 * 1000,
            "fundingRate": "-0.0002",
            "markPrice": "101.5",
            "rateType": "Special",
        },
    ]

    events = parse_funding_events(
        raw, symbol="BTCUSDT", observed_at=OBSERVED_AT + timedelta(hours=9)
    )

    assert len(events) == 2
    assert events[0].kind is MarketEventKind.FUNDING_SETTLEMENT
    assert events[0].sequence is None
    assert isinstance(events[0].payload, FundingSettlementPayload)
    assert events[0].payload.mark_price == Decimal("100.5")
    assert events[1].payload.rate_type == "Special"

    for field in ("fundingTime", "fundingRate", "markPrice", "rateType"):
        invalid = [dict(raw[0])]
        invalid[0].pop(field)
        with pytest.raises(BinanceContractError, match=field):
            parse_funding_events(invalid, symbol="BTCUSDT", observed_at=OBSERVED_AT)
    with pytest.raises(BinanceContractError, match="ascending"):
        parse_funding_events(tuple(reversed(raw)), symbol="BTCUSDT", observed_at=OBSERVED_AT)
