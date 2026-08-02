from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from maais.domain.enums import PaperOrderType
from maais.execution.paper.clock import require_utc
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.market_data.connectors.binance_contracts import (
    BINANCE_USDM_VENUE,
    BinanceContractError,
    BinanceDepthSnapshot,
)
from maais.market_data.events import (
    ClosedBarPayload,
    FundingSettlementPayload,
    MarketEventKind,
    ObservedMarketEvent,
    SymbolStatePayload,
    VenueClockPayload,
)

_INTERVAL_MILLISECONDS = {"1m": 60_000}
_REST_KLINE_STREAM = "{symbol}@kline_{interval}"


@dataclass(frozen=True, slots=True)
class BinancePublicPreflight:
    server_time: datetime
    observed_at: datetime
    request_weight_limit_per_minute: int
    venue_clocks: tuple[ObservedMarketEvent, ...]
    symbol_states: tuple[ObservedMarketEvent, ...]
    exchange_filters: tuple[ExchangeFilterSnapshot, ...]

    def __post_init__(self) -> None:
        require_utc(self.server_time, "preflight server_time")
        require_utc(self.observed_at, "preflight observed_at")
        if self.request_weight_limit_per_minute <= 0:
            raise ValueError("request weight limit must be positive")
        symbols = tuple(item.symbol for item in self.exchange_filters)
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("preflight filters require unique symbols")
        if tuple(item.symbol for item in self.venue_clocks) != symbols:
            raise ValueError("preflight clock coverage differs from filters")
        if tuple(item.symbol for item in self.symbol_states) != symbols:
            raise ValueError("preflight symbol-state coverage differs from filters")


def parse_server_time(
    raw: object,
    *,
    observed_at: datetime,
    symbol: str = "ALL",
) -> ObservedMarketEvent:
    require_utc(observed_at, "observed_at")
    _uppercase_symbol(symbol)
    data = _mapping(raw, "server-time response")
    server_ms = _integer(data, "serverTime")
    server_time = _milliseconds(server_ms)
    return ObservedMarketEvent(
        venue=BINANCE_USDM_VENUE,
        stream="rest:/fapi/v1/time",
        symbol=symbol,
        event_id=f"{BINANCE_USDM_VENUE}:server-time:{symbol}:{server_ms}",
        kind=MarketEventKind.VENUE_CLOCK,
        venue_event_at=server_time,
        observed_at=observed_at,
        sequence=None,
        sequence_not_applicable_reason="binance_server_time_has_no_sequence",
        payload=VenueClockPayload(server_time=server_time),
    )


def parse_exchange_info(
    raw: object,
    *,
    required_symbols: Sequence[str],
    server_time: datetime,
    observed_at: datetime,
) -> BinancePublicPreflight:
    require_utc(server_time, "server_time")
    require_utc(observed_at, "observed_at")
    symbols = tuple(required_symbols)
    if not symbols or len(set(symbols)) != len(symbols):
        raise BinanceContractError("required symbols must be nonempty and unique")
    for symbol in symbols:
        _uppercase_symbol(symbol)

    data = _mapping(raw, "exchange-info response")
    weight_limit = _request_weight_limit(data)
    rows = _mapping_rows(data, "symbols")
    indexed: dict[str, Mapping[str, object]] = {}
    for row in rows:
        symbol = _string(row, "symbol")
        if symbol in indexed:
            raise BinanceContractError(f"duplicate exchange symbol: {symbol}")
        indexed[symbol] = row
    missing = sorted(set(symbols) - indexed.keys())
    if missing:
        raise BinanceContractError(f"missing required symbols: {missing}")

    filters: list[ExchangeFilterSnapshot] = []
    states: list[ObservedMarketEvent] = []
    clocks: list[ObservedMarketEvent] = []
    server_ms = _datetime_milliseconds(server_time)
    for symbol in symbols:
        row = indexed[symbol]
        if _string(row, "contractType") != "PERPETUAL":
            raise BinanceContractError(f"required symbol is not a perpetual contract: {symbol}")
        if _string(row, "quoteAsset") != "USDT":
            raise BinanceContractError(f"required symbol is not USDT-quoted: {symbol}")
        status = _string(row, "status")
        filter_rows = _mapping_rows(row, "filters")
        by_type: dict[str, Mapping[str, object]] = {}
        for filter_row in filter_rows:
            filter_type = _string(filter_row, "filterType")
            if filter_type in by_type:
                raise BinanceContractError(
                    f"duplicate {filter_type} filter for required symbol {symbol}"
                )
            by_type[filter_type] = filter_row
        price = _named_filter(by_type, "PRICE_FILTER", symbol)
        lot = _named_filter(by_type, "LOT_SIZE", symbol)
        notional = _named_filter(by_type, "MIN_NOTIONAL", symbol)
        order_types = _strings(row, "orderTypes")
        supported = tuple(
            order_type
            for venue_name, order_type in (
                ("LIMIT", PaperOrderType.LIMIT),
                ("MARKET", PaperOrderType.MARKET),
            )
            if venue_name in order_types
        )
        if PaperOrderType.MARKET not in supported:
            raise BinanceContractError(f"required symbol lacks market-order support: {symbol}")
        filters.append(
            ExchangeFilterSnapshot(
                symbol=symbol,
                status=status,
                price_tick=_positive_decimal(price, "tickSize"),
                quantity_step=_positive_decimal(lot, "stepSize"),
                minimum_quantity=_positive_decimal(lot, "minQty"),
                maximum_quantity=_positive_decimal(lot, "maxQty"),
                minimum_notional=_positive_decimal(notional, "notional"),
                supported_order_types=supported,
                captured_at=observed_at,
            )
        )
        clocks.append(
            parse_server_time(
                {"serverTime": server_ms},
                observed_at=observed_at,
                symbol=symbol,
            )
        )
        states.append(
            ObservedMarketEvent(
                venue=BINANCE_USDM_VENUE,
                stream="rest:/fapi/v1/exchangeInfo",
                symbol=symbol,
                event_id=f"{BINANCE_USDM_VENUE}:symbol-state:{symbol}:{server_ms}:{status}",
                kind=MarketEventKind.SYMBOL_STATE,
                venue_event_at=server_time,
                observed_at=observed_at,
                sequence=None,
                sequence_not_applicable_reason="binance_exchange_info_has_no_sequence",
                payload=SymbolStatePayload(status=status),
            )
        )
    return BinancePublicPreflight(
        server_time=server_time,
        observed_at=observed_at,
        request_weight_limit_per_minute=weight_limit,
        venue_clocks=tuple(clocks),
        symbol_states=tuple(states),
        exchange_filters=tuple(filters),
    )


def parse_depth_snapshot(
    raw: object,
    *,
    symbol: str,
    observed_at: datetime,
) -> BinanceDepthSnapshot:
    _uppercase_symbol(symbol)
    require_utc(observed_at, "observed_at")
    data = _mapping(raw, "depth-snapshot response")
    published_at = _milliseconds(_integer(data, "E"))
    venue_event_at = _milliseconds(_integer(data, "T"))
    return BinanceDepthSnapshot(
        symbol=symbol,
        last_update_id=_integer(data, "lastUpdateId"),
        published_at=published_at,
        venue_event_at=venue_event_at,
        observed_at=observed_at,
        bids=_snapshot_levels(data, "bids"),
        asks=_snapshot_levels(data, "asks"),
    )


def parse_closed_bar_events(
    raw: object,
    *,
    symbol: str,
    interval: str,
    observed_at: datetime,
) -> tuple[ObservedMarketEvent, ...]:
    _uppercase_symbol(symbol)
    require_utc(observed_at, "observed_at")
    if interval not in _INTERVAL_MILLISECONDS:
        raise BinanceContractError(f"unsupported kline interval: {interval}")
    if not isinstance(raw, list):
        raise BinanceContractError("kline response must be an array")
    events: list[ObservedMarketEvent] = []
    previous_open: int | None = None
    interval_ms = _INTERVAL_MILLISECONDS[interval]
    stream = _REST_KLINE_STREAM.format(symbol=symbol.lower(), interval=interval)
    for index, value in enumerate(raw):
        if not isinstance(value, list) or len(value) != 12:
            raise BinanceContractError(f"kline row {index} must contain exactly 12 fields")
        open_ms = _list_integer(value, 0, f"kline[{index}].openTime")
        close_inclusive = _list_integer(value, 6, f"kline[{index}].closeTime")
        close_exclusive = close_inclusive + 1
        if close_exclusive - open_ms != interval_ms:
            raise BinanceContractError(f"kline row {index} has an invalid interval")
        if previous_open is not None and open_ms != previous_open + interval_ms:
            raise BinanceContractError("kline rows are not ascending and contiguous")
        previous_open = open_ms
        payload = ClosedBarPayload(
            timeframe=interval,
            bar_open_at=_milliseconds(open_ms),
            bar_close_at=_milliseconds(close_exclusive),
            open=_list_decimal(value, 1, f"kline[{index}].open"),
            high=_list_decimal(value, 2, f"kline[{index}].high"),
            low=_list_decimal(value, 3, f"kline[{index}].low"),
            close=_list_decimal(value, 4, f"kline[{index}].close"),
            volume=_list_decimal(value, 5, f"kline[{index}].volume"),
            quote_volume=_list_decimal(value, 7, f"kline[{index}].quoteVolume"),
            trade_count=_list_integer(value, 8, f"kline[{index}].tradeCount"),
            taker_buy_volume=_list_decimal(value, 9, f"kline[{index}].takerBuyVolume"),
            taker_buy_quote_volume=_list_decimal(
                value,
                10,
                f"kline[{index}].takerBuyQuoteVolume",
            ),
            closed=True,
        )
        events.append(
            ObservedMarketEvent(
                venue=BINANCE_USDM_VENUE,
                stream=stream,
                symbol=symbol,
                event_id=f"{BINANCE_USDM_VENUE}:{stream}:{symbol}:bar:{open_ms}",
                kind=MarketEventKind.CLOSED_BAR,
                venue_event_at=_milliseconds(close_exclusive),
                observed_at=observed_at,
                sequence=open_ms // interval_ms,
                sequence_not_applicable_reason=None,
                payload=payload,
            )
        )
    return tuple(events)


def parse_funding_events(
    raw: object,
    *,
    symbol: str,
    observed_at: datetime,
) -> tuple[ObservedMarketEvent, ...]:
    _uppercase_symbol(symbol)
    require_utc(observed_at, "observed_at")
    if not isinstance(raw, (list, tuple)):
        raise BinanceContractError("funding response must be an array")
    rows = tuple(_mapping(item, f"funding row {index}") for index, item in enumerate(raw))
    times = tuple(_integer(row, "fundingTime") for row in rows)
    if times != tuple(sorted(times)) or len(set(times)) != len(times):
        raise BinanceContractError("funding history must be strictly ascending")
    events: list[ObservedMarketEvent] = []
    for row, funding_ms in zip(rows, times):
        if _string(row, "symbol") != symbol:
            raise BinanceContractError("funding row symbol differs from the request")
        funding_at = _milliseconds(funding_ms)
        if observed_at < funding_at:
            raise BinanceContractError("funding settlement was observed before venue time")
        rate_type = _string(row, "rateType")
        payload = FundingSettlementPayload(
            funding_at=funding_at,
            funding_rate=_decimal(row, "fundingRate"),
            mark_price=_positive_decimal(row, "markPrice"),
            rate_type=rate_type,
        )
        events.append(
            ObservedMarketEvent(
                venue=BINANCE_USDM_VENUE,
                stream="rest:/fapi/v1/fundingRate",
                symbol=symbol,
                event_id=(f"{BINANCE_USDM_VENUE}:funding:{symbol}:{funding_ms}:{rate_type}"),
                kind=MarketEventKind.FUNDING_SETTLEMENT,
                venue_event_at=funding_at,
                observed_at=observed_at,
                sequence=None,
                sequence_not_applicable_reason="binance_funding_history_has_no_sequence",
                payload=payload,
            )
        )
    return tuple(events)


def _request_weight_limit(data: Mapping[str, object]) -> int:
    rows = _mapping_rows(data, "rateLimits")
    matches = [
        row
        for row in rows
        if _string(row, "rateLimitType") == "REQUEST_WEIGHT"
        and _string(row, "interval") == "MINUTE"
        and _integer(row, "intervalNum") == 1
    ]
    if len(matches) != 1:
        raise BinanceContractError("exchange info requires one per-minute request-weight limit")
    limit = _integer(matches[0], "limit")
    if limit <= 0:
        raise BinanceContractError("request-weight limit must be positive")
    return limit


def _named_filter(
    filters: Mapping[str, Mapping[str, object]],
    name: str,
    symbol: str,
) -> Mapping[str, object]:
    try:
        return filters[name]
    except KeyError as exc:
        raise BinanceContractError(f"required {name} filter is missing for {symbol}") from exc


def _snapshot_levels(
    data: Mapping[str, object],
    name: str,
) -> tuple[tuple[Decimal, Decimal], ...]:
    value = _required(data, name)
    if not isinstance(value, list):
        raise BinanceContractError(f"{name} must be an array")
    levels: list[tuple[Decimal, Decimal]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, list) or len(raw) != 2:
            raise BinanceContractError(f"{name}[{index}] must contain price and quantity")
        price = _decimal_value(raw[0], f"{name}[{index}].price")
        quantity = _decimal_value(raw[1], f"{name}[{index}].quantity")
        levels.append((price, quantity))
    return tuple(levels)


def _mapping_rows(data: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = _required(data, name)
    if not isinstance(value, list):
        raise BinanceContractError(f"{name} must be an array")
    return tuple(_mapping(item, f"{name} row") for item in value)


def _strings(data: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = _required(data, name)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BinanceContractError(f"{name} must be an array of strings")
    return tuple(value)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BinanceContractError(f"{field} must be an object")
    return value


def _required(data: Mapping[str, object], name: str) -> object:
    if name not in data or data[name] is None:
        raise BinanceContractError(f"required field is missing: {name}")
    return data[name]


def _string(data: Mapping[str, object], name: str) -> str:
    value = _required(data, name)
    if not isinstance(value, str):
        raise BinanceContractError(f"{name} must be a string")
    return value


def _integer(data: Mapping[str, object], name: str) -> int:
    value = _required(data, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BinanceContractError(f"{name} must be an integer")
    return value


def _decimal(data: Mapping[str, object], name: str) -> Decimal:
    return _decimal_value(_required(data, name), name)


def _positive_decimal(data: Mapping[str, object], name: str) -> Decimal:
    value = _decimal(data, name)
    if value <= 0:
        raise BinanceContractError(f"{name} must be positive")
    return value


def _decimal_value(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise BinanceContractError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BinanceContractError(f"{field} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise BinanceContractError(f"{field} must be finite")
    return parsed


def _list_integer(values: list[object], index: int, field: str) -> int:
    value = values[index]
    if not isinstance(value, int) or isinstance(value, bool):
        raise BinanceContractError(f"{field} must be an integer")
    return value


def _list_decimal(values: list[object], index: int, field: str) -> Decimal:
    return _decimal_value(values[index], field)


def _uppercase_symbol(symbol: str) -> None:
    if not symbol or symbol != symbol.upper() or not symbol.isalnum():
        raise BinanceContractError("symbol must be uppercase alphanumeric")


def _milliseconds(value: int) -> datetime:
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise BinanceContractError("timestamp milliseconds are out of range") from exc


def _datetime_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)
