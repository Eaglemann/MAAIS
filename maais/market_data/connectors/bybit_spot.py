"""Keyless Bybit spot reference adapter used as an independent venue check."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from maais.execution.paper.clock import require_utc
from maais.market_data.events import (
    MarketEventKind,
    ObservedMarketEvent,
    ReferenceKind,
    ReferencePricePayload,
)

PUBLIC_BYBIT_API_BASE_URL = "https://api.bybit.com"
BYBIT_SPOT_VENUE = "bybit_spot"
_CONSERVATIVE_PUBLIC_REQUESTS_PER_SECOND = 20
Sleep = Callable[[float], Awaitable[None]]


class BybitContractError(ValueError):
    """A Bybit public payload differs from the pinned official shape."""


@dataclass(frozen=True, slots=True)
class BybitSymbolMapping:
    primary_symbol: str
    bybit_symbol: str
    base_coin: str
    quote_coin: str
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
                self.bybit_symbol,
                self.base_coin,
                self.quote_coin,
                self.status,
            )
        ):
            raise ValueError("Bybit symbol mapping identity is required")


class _Pacer:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        sleep: Sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._monotonic()
            if self._last is not None:
                wait = 1 / _CONSERVATIVE_PUBLIC_REQUESTS_PER_SECOND - (now - self._last)
                if wait > 0:
                    await self._sleep(wait)
                    now = self._monotonic()
            self._last = now


def parse_bybit_server_time(raw: object) -> datetime:
    envelope, result = _envelope(raw)
    second = _integer_string(result, "timeSecond")
    nanosecond = _integer_string(result, "timeNano")
    envelope_ms = _integer(envelope, "time")
    if second != envelope_ms // 1000 or nanosecond // 1_000_000 != envelope_ms:
        raise BybitContractError("Bybit server-time fields disagree")
    return _milliseconds(envelope_ms)


def parse_bybit_instruments(
    raw: object,
    *,
    required_symbols: Sequence[str],
    server_time: datetime,
    observed_at: datetime,
) -> tuple[BybitSymbolMapping, ...]:
    require_utc(server_time, "server_time")
    require_utc(observed_at, "observed_at")
    required = tuple(required_symbols)
    if not required or len(set(required)) != len(required):
        raise BybitContractError("required Bybit symbols must be nonempty and unique")
    for symbol in required:
        _uppercase_symbol(symbol)
    _, result = _envelope(raw)
    if _string(result, "category") != "spot":
        raise BybitContractError("Bybit instrument response is not spot")
    rows = _mapping_rows(result, "list")
    indexed: dict[str, Mapping[str, object]] = {}
    for row in rows:
        symbol = _string(row, "symbol")
        if symbol in indexed:
            raise BybitContractError(f"duplicate Bybit spot instrument: {symbol}")
        indexed[symbol] = row
    missing = sorted(set(required) - indexed.keys())
    if missing:
        raise BybitContractError(f"missing required Bybit spot instruments: {missing}")
    mappings: list[BybitSymbolMapping] = []
    for symbol in required:
        row = indexed[symbol]
        base_coin = _string(row, "baseCoin")
        quote_coin = _string(row, "quoteCoin")
        status = _string(row, "status")
        if quote_coin != "USDT" or base_coin != symbol.removesuffix("USDT"):
            raise BybitContractError(f"Bybit spot mapping differs for {symbol}")
        if status != "Trading":
            raise BybitContractError(f"Bybit spot instrument is not Trading: {symbol}")
        mappings.append(
            BybitSymbolMapping(
                primary_symbol=symbol,
                bybit_symbol=symbol,
                base_coin=base_coin,
                quote_coin=quote_coin,
                status=status,
                server_time=server_time,
                observed_at=observed_at,
            )
        )
    return tuple(mappings)


def parse_bybit_reference_book(
    raw: object,
    *,
    primary_symbol: str,
    bybit_symbol: str,
    observed_at: datetime,
) -> ObservedMarketEvent:
    _uppercase_symbol(primary_symbol)
    _uppercase_symbol(bybit_symbol)
    require_utc(observed_at, "observed_at")
    _, result = _envelope(raw)
    if _string(result, "s") != bybit_symbol:
        raise BybitContractError("Bybit order book symbol differs from mapping")
    bids = _book_levels(result, "b")
    asks = _book_levels(result, "a")
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_bid >= best_ask:
        raise BybitContractError("Bybit reference order book is crossed or locked")
    update_id = _integer(result, "u")
    sequence = _integer(result, "seq")
    published_at = _milliseconds(_integer(result, "ts"))
    engine_at = _milliseconds(_integer(result, "cts"))
    source_event_id = f"{update_id}:{sequence}"
    return ObservedMarketEvent(
        venue=BYBIT_SPOT_VENUE,
        stream="rest:/v5/market/orderbook",
        symbol=primary_symbol,
        event_id=f"{BYBIT_SPOT_VENUE}:orderbook:{bybit_symbol}:{source_event_id}",
        kind=MarketEventKind.REFERENCE_PRICE,
        venue_event_at=engine_at,
        observed_at=observed_at,
        sequence=sequence,
        sequence_not_applicable_reason=None,
        payload=ReferencePricePayload(
            reference_kind=ReferenceKind.SECONDARY_VENUE,
            instrument=bybit_symbol,
            price=(best_bid + best_ask) / Decimal("2"),
            source_event_id=source_event_id,
            source_quantity=None,
            source_side=None,
            source_bid=best_bid,
            source_ask=best_ask,
            source_published_at=published_at,
        ),
    )


class BybitSpotConnector:
    """Preflighted public REST polling adapter for independent spot references."""

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
        self._pacer = _Pacer(monotonic=monotonic, sleep=sleep)
        self._mappings: dict[str, BybitSymbolMapping] = {}

    async def __aenter__(self) -> BybitSpotConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=PUBLIC_BYBIT_API_BASE_URL,
                timeout=15.0,
                http2=True,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def preflight_complete(self) -> bool:
        return bool(self._mappings)

    @property
    def mappings(self) -> tuple[BybitSymbolMapping, ...]:
        if not self._mappings:
            raise RuntimeError("Bybit public preflight has not completed")
        return tuple(self._mappings.values())

    async def preflight(
        self,
        required_symbols: Sequence[str],
    ) -> tuple[BybitSymbolMapping, ...]:
        server_raw, _ = await self._get_json("/v5/market/time", {})
        server_time = parse_bybit_server_time(server_raw)
        instruments_raw, observed_at = await self._get_json(
            "/v5/market/instruments-info",
            {"category": "spot"},
        )
        mappings = parse_bybit_instruments(
            instruments_raw,
            required_symbols=required_symbols,
            server_time=server_time,
            observed_at=observed_at,
        )
        self._mappings = {item.primary_symbol: item for item in mappings}
        return mappings

    async def get_reference_event(self, primary_symbol: str) -> ObservedMarketEvent:
        try:
            mapping = self._mappings[primary_symbol]
        except KeyError as exc:
            if not self._mappings:
                raise RuntimeError("Bybit public preflight has not completed") from exc
            raise RuntimeError(
                f"symbol was not admitted by Bybit preflight: {primary_symbol}"
            ) from exc
        raw, observed_at = await self._get_json(
            "/v5/market/orderbook",
            {"category": "spot", "symbol": mapping.bybit_symbol, "limit": 1},
        )
        return parse_bybit_reference_book(
            raw,
            primary_symbol=primary_symbol,
            bybit_symbol=mapping.bybit_symbol,
            observed_at=observed_at,
        )

    async def get_reference_events(self) -> tuple[ObservedMarketEvent, ...]:
        if not self._mappings:
            raise RuntimeError("Bybit public preflight has not completed")
        return tuple(
            await asyncio.gather(*(self.get_reference_event(symbol) for symbol in self._mappings))
        )

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, str | int],
    ) -> tuple[object, datetime]:
        if self._client is None:
            raise RuntimeError("BybitSpotConnector must be used as an async context manager")
        await self._pacer.acquire()
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        observed_at = self._observed_now()
        require_utc(observed_at, "observed_now")
        try:
            return response.json(), observed_at
        except ValueError as exc:
            raise BybitContractError(f"{path} did not return valid JSON") from exc


def _envelope(raw: object) -> tuple[Mapping[str, object], Mapping[str, object]]:
    data = _mapping(raw, "Bybit response")
    if _integer(data, "retCode") != 0 or _string(data, "retMsg") != "OK":
        raise BybitContractError(
            f"Bybit response failed: retCode={data.get('retCode')} retMsg={data.get('retMsg')}"
        )
    _integer(data, "time")
    _mapping(_required(data, "retExtInfo"), "retExtInfo")
    result = _mapping(_required(data, "result"), "result")
    return data, result


def _mapping_rows(data: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = _required(data, name)
    if not isinstance(value, list):
        raise BybitContractError(f"{name} must be an array")
    return tuple(_mapping(item, f"{name} row") for item in value)


def _book_levels(
    data: Mapping[str, object],
    name: str,
) -> tuple[tuple[Decimal, Decimal], ...]:
    value = _required(data, name)
    if not isinstance(value, list) or not value:
        raise BybitContractError(f"{name} must be a nonempty book array")
    levels: list[tuple[Decimal, Decimal]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, list) or len(raw) != 2:
            raise BybitContractError(f"{name}[{index}] must contain price and quantity")
        price = _positive_decimal_value(raw[0], f"{name}[{index}].price")
        quantity = _positive_decimal_value(raw[1], f"{name}[{index}].quantity")
        levels.append((price, quantity))
    return tuple(levels)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BybitContractError(f"{field} must be an object")
    return value


def _required(data: Mapping[str, object], name: str) -> object:
    if name not in data or data[name] is None:
        raise BybitContractError(f"required field is missing: {name}")
    return data[name]


def _string(data: Mapping[str, object], name: str) -> str:
    value = _required(data, name)
    if not isinstance(value, str):
        raise BybitContractError(f"{name} must be a string")
    return value


def _integer(data: Mapping[str, object], name: str) -> int:
    value = _required(data, name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BybitContractError(f"{name} must be an integer")
    return value


def _integer_string(data: Mapping[str, object], name: str) -> int:
    value = _string(data, name)
    if not value.isdigit():
        raise BybitContractError(f"{name} must be an integer string")
    return int(value)


def _positive_decimal_value(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise BybitContractError(f"{name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BybitContractError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BybitContractError(f"{name} must be positive and finite")
    return parsed


def _uppercase_symbol(symbol: str) -> None:
    if not symbol or symbol != symbol.upper() or not symbol.isalnum():
        raise BybitContractError("symbol must be uppercase alphanumeric")


def _milliseconds(value: int) -> datetime:
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise BybitContractError("timestamp milliseconds are out of range") from exc
