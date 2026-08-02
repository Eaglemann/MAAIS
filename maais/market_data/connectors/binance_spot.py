"""Keyless Binance Spot reference adapter on the low-latency public origin."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from maais.execution.paper.clock import require_utc
from maais.market_data.connectors.binance_rest import RequestPacer
from maais.market_data.events import (
    MarketEventKind,
    ObservedMarketEvent,
    ReferenceKind,
    ReferencePricePayload,
)

PUBLIC_BINANCE_SPOT_API_BASE_URL = "https://api.binance.com"
BINANCE_SPOT_VENUE = "binance_spot"
_PROVISIONAL_WEIGHT_LIMIT_PER_MINUTE = 1200
_EXCHANGE_INFO_WEIGHT = 20
_BOOK_TICKER_BATCH_WEIGHT = 4
Sleep = Callable[[float], Awaitable[None]]


class BinanceSpotContractError(ValueError):
    """A Binance Spot public payload differs from the pinned official shape."""


@dataclass(frozen=True, slots=True)
class BinanceSpotSymbolMapping:
    primary_symbol: str
    spot_symbol: str
    base_asset: str
    quote_asset: str
    status: str
    server_time: datetime
    observed_at: datetime

    def __post_init__(self) -> None:
        for value, field in (
            (self.server_time, "mapping server_time"),
            (self.observed_at, "mapping observed_at"),
        ):
            require_utc(value, field)
        if not all(
            (
                self.primary_symbol,
                self.spot_symbol,
                self.base_asset,
                self.quote_asset,
                self.status,
            )
        ):
            raise ValueError("Binance Spot symbol mapping identity is required")


@dataclass(frozen=True, slots=True)
class BinanceSpotPreflight:
    server_time: datetime
    observed_at: datetime
    request_weight_limit_per_minute: int
    mappings: tuple[BinanceSpotSymbolMapping, ...]

    def __post_init__(self) -> None:
        require_utc(self.server_time, "preflight server_time")
        require_utc(self.observed_at, "preflight observed_at")
        if self.request_weight_limit_per_minute <= 0:
            raise ValueError("request weight limit must be positive")
        symbols = tuple(item.primary_symbol for item in self.mappings)
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("preflight mappings require unique symbols")


def parse_binance_spot_server_time(raw: object) -> datetime:
    data = _mapping(raw, "Binance Spot server-time response")
    return _milliseconds(_integer(data, "serverTime"))


def parse_binance_spot_exchange_info(
    raw: object,
    *,
    required_symbols: Sequence[str],
    server_time: datetime,
    observed_at: datetime,
) -> BinanceSpotPreflight:
    require_utc(server_time, "server_time")
    require_utc(observed_at, "observed_at")
    required = tuple(required_symbols)
    if not required or len(set(required)) != len(required):
        raise BinanceSpotContractError("required Binance Spot symbols must be nonempty and unique")
    for symbol in required:
        _uppercase_symbol(symbol)

    data = _mapping(raw, "Binance Spot exchange-info response")
    if _string(data, "timezone") != "UTC":
        raise BinanceSpotContractError("Binance Spot exchange timezone is not UTC")
    exchange_server_time = _milliseconds(_integer(data, "serverTime"))
    if exchange_server_time < server_time:
        raise BinanceSpotContractError("Binance Spot exchange-info clock moved backwards")
    weight_limit = _request_weight_limit(data)
    indexed: dict[str, Mapping[str, object]] = {}
    for row in _mapping_rows(data, "symbols"):
        symbol = _string(row, "symbol")
        if symbol in indexed:
            raise BinanceSpotContractError(f"duplicate Binance Spot instrument: {symbol}")
        indexed[symbol] = row
    missing = sorted(set(required) - indexed.keys())
    if missing:
        raise BinanceSpotContractError(f"missing required Binance Spot instruments: {missing}")

    mappings: list[BinanceSpotSymbolMapping] = []
    for symbol in required:
        row = indexed[symbol]
        base_asset = _string(row, "baseAsset")
        quote_asset = _string(row, "quoteAsset")
        status = _string(row, "status")
        if quote_asset != "USDT" or base_asset != symbol.removesuffix("USDT"):
            raise BinanceSpotContractError(f"Binance Spot base/quote mapping differs for {symbol}")
        if status != "TRADING":
            raise BinanceSpotContractError(f"Binance Spot instrument is not TRADING: {symbol}")
        if _boolean(row, "isSpotTradingAllowed") is not True:
            raise BinanceSpotContractError(f"Binance Spot trading is not enabled for {symbol}")
        order_types = _strings(row, "orderTypes")
        if "MARKET" not in order_types:
            raise BinanceSpotContractError(
                f"Binance Spot instrument lacks MARKET support: {symbol}"
            )
        mappings.append(
            BinanceSpotSymbolMapping(
                primary_symbol=symbol,
                spot_symbol=symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                status=status,
                server_time=server_time,
                observed_at=observed_at,
            )
        )
    return BinanceSpotPreflight(
        server_time=server_time,
        observed_at=observed_at,
        request_weight_limit_per_minute=weight_limit,
        mappings=tuple(mappings),
    )


def parse_binance_spot_book_tickers(
    raw: object,
    *,
    mappings: Sequence[BinanceSpotSymbolMapping],
    observed_at: datetime,
) -> tuple[ObservedMarketEvent, ...]:
    require_utc(observed_at, "observed_at")
    admitted = tuple(mappings)
    if not admitted:
        raise BinanceSpotContractError("Binance Spot book-ticker mappings cannot be empty")
    required = tuple(item.spot_symbol for item in admitted)
    if len(set(required)) != len(required):
        raise BinanceSpotContractError("Binance Spot book-ticker mappings must be unique")
    rows = _mapping_array(raw, "Binance Spot book-ticker response")
    indexed: dict[str, Mapping[str, object]] = {}
    for row in rows:
        symbol = _string(row, "symbol")
        if symbol in indexed:
            raise BinanceSpotContractError(f"duplicate Binance Spot book ticker: {symbol}")
        indexed[symbol] = row
    unexpected = sorted(indexed.keys() - set(required))
    if unexpected:
        raise BinanceSpotContractError(f"unexpected Binance Spot book tickers: {unexpected}")
    missing = sorted(set(required) - indexed.keys())
    if missing:
        raise BinanceSpotContractError(f"missing Binance Spot book tickers: {missing}")

    events: list[ObservedMarketEvent] = []
    for mapping in admitted:
        row = indexed[mapping.spot_symbol]
        bid = _decimal(row, "bidPrice", positive=True)
        _decimal(row, "bidQty", positive=True)
        ask = _decimal(row, "askPrice", positive=True)
        _decimal(row, "askQty", positive=True)
        if bid >= ask:
            raise BinanceSpotContractError(
                f"Binance Spot reference quote is crossed or locked: {mapping.spot_symbol}"
            )
        bid_raw = _string(row, "bidPrice")
        bid_quantity_raw = _string(row, "bidQty")
        ask_raw = _string(row, "askPrice")
        ask_quantity_raw = _string(row, "askQty")
        observed_us = _datetime_microseconds(observed_at)
        source_event_id = f"{bid_raw}:{bid_quantity_raw}:{ask_raw}:{ask_quantity_raw}:{observed_us}"
        events.append(
            ObservedMarketEvent(
                venue=BINANCE_SPOT_VENUE,
                stream="rest:/api/v3/ticker/bookTicker",
                symbol=mapping.primary_symbol,
                event_id=(
                    f"{BINANCE_SPOT_VENUE}:bookTicker:{mapping.spot_symbol}:{source_event_id}"
                ),
                kind=MarketEventKind.REFERENCE_PRICE,
                venue_event_at=observed_at,
                observed_at=observed_at,
                sequence=None,
                sequence_not_applicable_reason=("binance_spot_book_ticker_has_no_sequence"),
                payload=ReferencePricePayload(
                    reference_kind=ReferenceKind.PRIMARY_SPOT,
                    instrument=mapping.spot_symbol,
                    price=(bid + ask) / Decimal("2"),
                    source_event_id=source_event_id,
                    source_quantity=None,
                    source_side=None,
                    source_bid=bid,
                    source_ask=ask,
                    source_published_at=None,
                ),
            )
        )
    return tuple(events)


def _datetime_microseconds(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = value - epoch
    return elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds


class BinanceSpotConnector:
    """Preflighted public REST polling adapter for primary spot references."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        observed_now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._observed_now = observed_now or (lambda: datetime.now(timezone.utc))
        self._weight = RequestPacer(
            _PROVISIONAL_WEIGHT_LIMIT_PER_MINUTE,
            60,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._preflight: BinanceSpotPreflight | None = None

    async def __aenter__(self) -> BinanceSpotConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=PUBLIC_BINANCE_SPOT_API_BASE_URL,
                timeout=15.0,
                http2=True,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def request_weight_limit_per_minute(self) -> int:
        return self._weight.limit

    @property
    def preflight_complete(self) -> bool:
        return self._preflight is not None

    @property
    def preflight_result(self) -> BinanceSpotPreflight:
        if self._preflight is None:
            raise RuntimeError("Binance Spot public preflight has not completed")
        return self._preflight

    async def preflight(
        self,
        required_symbols: Sequence[str],
    ) -> BinanceSpotPreflight:
        symbols = tuple(required_symbols)
        server_raw, _ = await self._get_json("/api/v3/time", {}, weight=1)
        server_time = parse_binance_spot_server_time(server_raw)
        exchange_raw, observed_at = await self._get_json(
            "/api/v3/exchangeInfo",
            {"symbols": _json_symbols(symbols)},
            weight=_EXCHANGE_INFO_WEIGHT,
        )
        result = parse_binance_spot_exchange_info(
            exchange_raw,
            required_symbols=symbols,
            server_time=server_time,
            observed_at=observed_at,
        )
        self._weight.update_limit(result.request_weight_limit_per_minute)
        self._preflight = result
        return result

    async def get_reference_events(self) -> tuple[ObservedMarketEvent, ...]:
        preflight = self.preflight_result
        raw, observed_at = await self._get_json(
            "/api/v3/ticker/bookTicker",
            {
                "symbols": _json_symbols(tuple(item.spot_symbol for item in preflight.mappings)),
                "symbolStatus": "TRADING",
            },
            weight=_BOOK_TICKER_BATCH_WEIGHT,
        )
        return parse_binance_spot_book_tickers(
            raw,
            mappings=preflight.mappings,
            observed_at=observed_at,
        )

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, str | int],
        *,
        weight: int,
    ) -> tuple[object, datetime]:
        if self._client is None:
            raise RuntimeError("BinanceSpotConnector must be used as an async context manager")
        await self._weight.acquire(weight)
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        observed_at = self._observed_now()
        require_utc(observed_at, "observed_now")
        try:
            return response.json(), observed_at
        except ValueError as exc:
            raise BinanceSpotContractError(f"{path} did not return valid JSON") from exc


def _request_weight_limit(data: Mapping[str, object]) -> int:
    limits: list[int] = []
    for row in _mapping_rows(data, "rateLimits"):
        if (
            _string(row, "rateLimitType") == "REQUEST_WEIGHT"
            and _string(row, "interval") == "MINUTE"
            and _integer(row, "intervalNum") == 1
        ):
            limits.append(_integer(row, "limit"))
    if len(limits) != 1 or limits[0] <= 0:
        raise BinanceSpotContractError(
            "Binance Spot exchangeInfo must advertise one positive minute weight limit"
        )
    return limits[0]


def _json_symbols(symbols: Sequence[str]) -> str:
    if not symbols:
        raise BinanceSpotContractError("Binance Spot symbols cannot be empty")
    for symbol in symbols:
        _uppercase_symbol(symbol)
    return json.dumps(tuple(symbols), separators=(",", ":"))


def _mapping_array(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise BinanceSpotContractError(f"{field} must be an array")
    return tuple(_mapping(item, f"{field} row") for item in value)


def _mapping_rows(
    data: Mapping[str, object],
    name: str,
) -> tuple[Mapping[str, object], ...]:
    return _mapping_array(_required(data, name), name)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BinanceSpotContractError(f"{field} must be an object")
    return value


def _required(data: Mapping[str, object], name: str) -> object:
    if name not in data or data[name] is None:
        raise BinanceSpotContractError(f"required field is missing: {name}")
    return data[name]


def _string(data: Mapping[str, object], name: str) -> str:
    value = _required(data, name)
    if not isinstance(value, str) or not value:
        raise BinanceSpotContractError(f"{name} must be a nonempty string")
    return value


def _strings(data: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = _required(data, name)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise BinanceSpotContractError(f"{name} must be a nonempty string array")
    return tuple(value)


def _integer(data: Mapping[str, object], name: str) -> int:
    value = _required(data, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BinanceSpotContractError(f"{name} must be an integer")
    return value


def _boolean(data: Mapping[str, object], name: str) -> bool:
    value = _required(data, name)
    if not isinstance(value, bool):
        raise BinanceSpotContractError(f"{name} must be a boolean")
    return value


def _decimal(
    data: Mapping[str, object],
    name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    value = _string(data, name)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BinanceSpotContractError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise BinanceSpotContractError(f"{name} must be finite")
    if positive and parsed <= 0:
        raise BinanceSpotContractError(f"{name} must be positive")
    if nonnegative and parsed < 0:
        raise BinanceSpotContractError(f"{name} must be nonnegative")
    return parsed


def _uppercase_symbol(symbol: str) -> None:
    if not symbol or symbol != symbol.upper() or not symbol.isalnum():
        raise BinanceSpotContractError("symbol must be uppercase alphanumeric")


def _milliseconds(value: int) -> datetime:
    if value < 0:
        raise BinanceSpotContractError("timestamp milliseconds must be nonnegative")
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise BinanceSpotContractError("timestamp milliseconds are out of range") from exc
