from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from maais.execution.paper.clock import require_utc
from maais.market_data.events import (
    AggressorSide,
    ClosedBarPayload,
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
    OrderBookPayload,
    PriceLevel,
    TradePayload,
)

BINANCE_USDM_VENUE = "binance_usdm"
_SUPPORTED_DEPTH_SPEEDS = frozenset({"100ms", "250ms", "500ms"})
_INTERVAL_MILLISECONDS = {"1m": 60_000}


class BinanceContractError(ValueError):
    """The public venue payload differs from the pinned contract."""


class BinanceSequenceGap(RuntimeError):
    """A USD-M depth delta cannot follow the current official book."""


@dataclass(frozen=True, slots=True)
class DepthChange:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if not self.price.is_finite() or self.price <= 0:
            raise BinanceContractError("depth price must be positive")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise BinanceContractError("depth quantity must be nonnegative")


@dataclass(frozen=True, slots=True)
class BinanceDepthDelta:
    stream: str
    symbol: str
    event_at: datetime
    transaction_at: datetime
    observed_at: datetime
    first_update_id: int
    final_update_id: int
    previous_final_update_id: int
    bids: tuple[DepthChange, ...]
    asks: tuple[DepthChange, ...]

    def __post_init__(self) -> None:
        for value, field in (
            (self.event_at, "depth event_at"),
            (self.transaction_at, "depth transaction_at"),
            (self.observed_at, "depth observed_at"),
        ):
            require_utc(value, field)
        if not self.stream or not self.symbol or self.symbol != self.symbol.upper():
            raise BinanceContractError("depth stream and uppercase symbol are required")
        if (
            min(
                self.first_update_id,
                self.final_update_id,
                self.previous_final_update_id,
            )
            < 0
        ):
            raise BinanceContractError("depth update IDs must be nonnegative")
        if self.final_update_id < self.first_update_id:
            raise BinanceContractError("depth final update precedes its first update")


@dataclass(frozen=True, slots=True)
class BinanceDepthSnapshot:
    symbol: str
    last_update_id: int
    published_at: datetime
    venue_event_at: datetime
    observed_at: datetime
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]

    def __post_init__(self) -> None:
        require_utc(self.published_at, "snapshot published_at")
        require_utc(self.venue_event_at, "snapshot venue_event_at")
        require_utc(self.observed_at, "snapshot observed_at")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise BinanceContractError("snapshot symbol must be uppercase")
        if self.last_update_id < 0:
            raise BinanceContractError("snapshot update ID must be nonnegative")
        _validate_snapshot_side(self.bids, descending=True, side="bids")
        _validate_snapshot_side(self.asks, descending=False, side="asks")
        if self.bids[0][0] >= self.asks[0][0]:
            raise BinanceContractError("snapshot is crossed or locked")


class BinanceDepthBook:
    """Reconciles official USD-M REST snapshots with diff-depth update ranges."""

    def __init__(self, snapshot: BinanceDepthSnapshot, depth: int) -> None:
        if depth <= 0:
            raise ValueError("published depth must be positive")
        self._symbol = snapshot.symbol
        self._snapshot_sequence = snapshot.last_update_id
        self._current_sequence = snapshot.last_update_id
        self._bids = dict(snapshot.bids)
        self._asks = dict(snapshot.asks)
        self._depth = depth
        self._started = False

    @classmethod
    def from_snapshot(
        cls,
        snapshot: BinanceDepthSnapshot,
        *,
        depth: int,
    ) -> BinanceDepthBook:
        return cls(snapshot, depth)

    @property
    def current_sequence(self) -> int:
        return self._current_sequence

    def apply(self, delta: BinanceDepthDelta) -> ObservedMarketEvent | None:
        if delta.symbol != self._symbol:
            raise BinanceContractError("depth delta and snapshot symbols differ")
        if delta.final_update_id <= self._current_sequence:
            return None

        expected = self._current_sequence + 1
        covers_next = delta.first_update_id <= expected <= delta.final_update_id
        overlaps_current = delta.first_update_id <= self._current_sequence <= delta.final_update_id
        if not covers_next and not overlaps_current:
            raise BinanceSequenceGap(
                "depth first/final update range does not cover the next official update"
            )
        if self._started and delta.previous_final_update_id != self._current_sequence:
            raise BinanceSequenceGap(
                "depth previous final update does not match the current official book"
            )
        if (
            not self._started
            and not overlaps_current
            and delta.previous_final_update_id != self._current_sequence
        ):
            raise BinanceSequenceGap(
                "first depth previous final update does not match the REST snapshot"
            )

        _apply_changes(self._bids, delta.bids)
        _apply_changes(self._asks, delta.asks)
        self._current_sequence = delta.final_update_id
        self._started = True
        bids = _published_levels(self._bids, depth=self._depth, descending=True)
        asks = _published_levels(self._asks, depth=self._depth, descending=False)
        try:
            payload = OrderBookPayload(
                bids=bids,
                asks=asks,
                published_at=delta.event_at,
                sequence_start=delta.first_update_id,
                previous_sequence=delta.previous_final_update_id,
                snapshot_sequence=self._snapshot_sequence,
            )
        except ValueError as exc:
            raise BinanceContractError(str(exc)) from exc
        return ObservedMarketEvent(
            venue=BINANCE_USDM_VENUE,
            stream=delta.stream,
            symbol=delta.symbol,
            event_id=(
                f"{BINANCE_USDM_VENUE}:{delta.stream}:{delta.symbol}:depth:{delta.final_update_id}"
            ),
            kind=MarketEventKind.ORDER_BOOK,
            venue_event_at=delta.transaction_at,
            observed_at=delta.observed_at,
            sequence=delta.final_update_id,
            sequence_not_applicable_reason=None,
            payload=payload,
        )


def parse_websocket_message(
    raw: str | bytes,
    *,
    observed_at: datetime,
) -> ObservedMarketEvent | BinanceDepthDelta | None:
    """Parse one pinned USD-M combined-stream payload without defaults."""

    require_utc(observed_at, "observed_at")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BinanceContractError("websocket message is not valid JSON") from exc
    message = _mapping(decoded, "combined websocket message")
    stream = _string(message, "stream")
    data = _mapping(_required(message, "data"), "combined websocket data")
    symbol = _stream_symbol(stream)
    parts = stream.split("@")
    is_aggregate_trade = len(parts) == 2 and parts[1] == "aggTrade"
    is_kline = len(parts) == 2 and parts[1].startswith("kline_")
    is_mark = len(parts) == 3 and parts[1] == "markPrice" and parts[2] == "1s"
    is_depth = len(parts) == 3 and parts[1] == "depth" and parts[2] in _SUPPORTED_DEPTH_SPEEDS
    if not any((is_aggregate_trade, is_kline, is_mark, is_depth)):
        raise BinanceContractError(f"unsupported stream contract: {stream}")
    if _string(data, "s") != symbol:
        raise BinanceContractError("stream and payload symbol differ")

    if is_aggregate_trade:
        return _parse_aggregate_trade(stream, symbol, data, observed_at)
    if is_kline:
        return _parse_kline(stream, symbol, parts[1][len("kline_") :], data, observed_at)
    if is_mark:
        return _parse_mark(stream, symbol, data, observed_at)
    assert is_depth
    return _parse_depth(stream, symbol, data, observed_at)


def _parse_kline(
    stream: str,
    symbol: str,
    interval: str,
    data: Mapping[str, object],
    observed_at: datetime,
) -> ObservedMarketEvent | None:
    _event_type(data, "kline")
    if interval not in _INTERVAL_MILLISECONDS:
        raise BinanceContractError(f"unsupported kline interval: {interval}")
    event_ms = _integer(data, "E")
    kline = _mapping(_required(data, "k"), "kline payload")
    if _string(kline, "s") != symbol or _string(kline, "i") != interval:
        raise BinanceContractError("kline stream identity differs from its payload")
    if not _boolean(kline, "x"):
        return None
    open_ms = _integer(kline, "t")
    close_inclusive_ms = _integer(kline, "T")
    close_exclusive_ms = close_inclusive_ms + 1
    if close_exclusive_ms - open_ms != _INTERVAL_MILLISECONDS[interval]:
        raise BinanceContractError("kline interval does not match open/close timestamps")
    payload = ClosedBarPayload(
        timeframe=interval,
        bar_open_at=_milliseconds(open_ms),
        bar_close_at=_milliseconds(close_exclusive_ms),
        open=_decimal(kline, "o"),
        high=_decimal(kline, "h"),
        low=_decimal(kline, "l"),
        close=_decimal(kline, "c"),
        volume=_decimal(kline, "v"),
        quote_volume=_decimal(kline, "q"),
        trade_count=_integer(kline, "n"),
        taker_buy_volume=_decimal(kline, "V"),
        taker_buy_quote_volume=_decimal(kline, "Q"),
        closed=True,
    )
    sequence = open_ms // _INTERVAL_MILLISECONDS[interval]
    return ObservedMarketEvent(
        venue=BINANCE_USDM_VENUE,
        stream=stream,
        symbol=symbol,
        event_id=f"{BINANCE_USDM_VENUE}:{stream}:{symbol}:bar:{open_ms}",
        kind=MarketEventKind.CLOSED_BAR,
        venue_event_at=_milliseconds(event_ms),
        observed_at=observed_at,
        sequence=sequence,
        sequence_not_applicable_reason=None,
        payload=payload,
    )


def _parse_mark(
    stream: str,
    symbol: str,
    data: Mapping[str, object],
    observed_at: datetime,
) -> ObservedMarketEvent:
    _event_type(data, "markPriceUpdate")
    event_ms = _integer(data, "E")
    estimated_raw = _string(data, "P")
    estimated = None if estimated_raw == "" else _decimal_string(estimated_raw, "P")
    return ObservedMarketEvent(
        venue=BINANCE_USDM_VENUE,
        stream=stream,
        symbol=symbol,
        event_id=f"{BINANCE_USDM_VENUE}:{stream}:{symbol}:mark:{event_ms}",
        kind=MarketEventKind.MARK_FUNDING,
        venue_event_at=_milliseconds(event_ms),
        observed_at=observed_at,
        sequence=None,
        sequence_not_applicable_reason="binance_mark_stream_has_no_sequence",
        payload=MarkFundingPayload(
            mark_price=_decimal(data, "p"),
            index_price=_decimal(data, "i"),
            funding_rate=_decimal(data, "r"),
            next_funding_at=_milliseconds(_integer(data, "T")),
            estimated_settle_price=estimated,
        ),
    )


def _parse_aggregate_trade(
    stream: str,
    symbol: str,
    data: Mapping[str, object],
    observed_at: datetime,
) -> ObservedMarketEvent:
    _event_type(data, "aggTrade")
    aggregate_id = _integer(data, "a")
    buyer_is_maker = _boolean(data, "m")
    return ObservedMarketEvent(
        venue=BINANCE_USDM_VENUE,
        stream=stream,
        symbol=symbol,
        event_id=f"{BINANCE_USDM_VENUE}:{stream}:{symbol}:agg:{aggregate_id}",
        kind=MarketEventKind.TRADE,
        venue_event_at=_milliseconds(_integer(data, "T")),
        observed_at=observed_at,
        sequence=aggregate_id,
        sequence_not_applicable_reason=None,
        payload=TradePayload(
            price=_decimal(data, "p"),
            quantity=_decimal(data, "q"),
            aggressor_side=AggressorSide.SELL if buyer_is_maker else AggressorSide.BUY,
        ),
    )


def _parse_depth(
    stream: str,
    symbol: str,
    data: Mapping[str, object],
    observed_at: datetime,
) -> BinanceDepthDelta:
    _event_type(data, "depthUpdate")
    return BinanceDepthDelta(
        stream=stream,
        symbol=symbol,
        event_at=_milliseconds(_integer(data, "E")),
        transaction_at=_milliseconds(_integer(data, "T")),
        observed_at=observed_at,
        first_update_id=_integer(data, "U"),
        final_update_id=_integer(data, "u"),
        previous_final_update_id=_integer(data, "pu"),
        bids=_changes(data, "b"),
        asks=_changes(data, "a"),
    )


def _validate_snapshot_side(
    levels: Sequence[tuple[Decimal, Decimal]],
    *,
    descending: bool,
    side: str,
) -> None:
    if not levels:
        raise BinanceContractError(f"snapshot {side} cannot be empty")
    for price, quantity in levels:
        if not price.is_finite() or price <= 0 or not quantity.is_finite() or quantity <= 0:
            raise BinanceContractError(f"snapshot {side} require positive finite levels")
    prices = [price for price, _ in levels]
    if len(set(prices)) != len(prices):
        raise BinanceContractError(f"snapshot {side} contain duplicate prices")
    if prices != sorted(prices, reverse=descending):
        raise BinanceContractError(f"snapshot {side} are not correctly sorted")


def _apply_changes(book: dict[Decimal, Decimal], changes: tuple[DepthChange, ...]) -> None:
    for change in changes:
        if change.quantity == 0:
            book.pop(change.price, None)
        else:
            book[change.price] = change.quantity


def _published_levels(
    book: Mapping[Decimal, Decimal],
    *,
    depth: int,
    descending: bool,
) -> tuple[PriceLevel, ...]:
    prices = sorted(book, reverse=descending)[:depth]
    return tuple(PriceLevel(price, book[price]) for price in prices)


def _changes(data: Mapping[str, object], name: str) -> tuple[DepthChange, ...]:
    value = _required(data, name)
    if not isinstance(value, list):
        raise BinanceContractError(f"{name} must be an array of depth changes")
    changes: list[DepthChange] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, list) or len(raw) != 2:
            raise BinanceContractError(f"{name}[{index}] must contain price and quantity")
        price = _decimal_value(raw[0], f"{name}[{index}].price")
        quantity = _decimal_value(raw[1], f"{name}[{index}].quantity")
        changes.append(DepthChange(price, quantity))
    return tuple(changes)


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


def _boolean(data: Mapping[str, object], name: str) -> bool:
    value = _required(data, name)
    if not isinstance(value, bool):
        raise BinanceContractError(f"{name} must be a boolean")
    return value


def _decimal(data: Mapping[str, object], name: str) -> Decimal:
    return _decimal_value(_required(data, name), name)


def _decimal_value(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise BinanceContractError(f"{field} must be a decimal string")
    return _decimal_string(value, field)


def _decimal_string(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BinanceContractError(f"{field} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise BinanceContractError(f"{field} must be finite")
    return parsed


def _event_type(data: Mapping[str, object], expected: str) -> None:
    if _string(data, "e") != expected:
        raise BinanceContractError(f"expected event type {expected}")


def _stream_symbol(stream: str) -> str:
    if "@" not in stream:
        raise BinanceContractError(f"unsupported stream contract: {stream}")
    raw_symbol = stream.split("@", 1)[0]
    if not raw_symbol or raw_symbol != raw_symbol.lower() or not raw_symbol.isalnum():
        raise BinanceContractError("stream symbol must be lowercase alphanumeric")
    return raw_symbol.upper()


def _milliseconds(value: int) -> datetime:
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise BinanceContractError("timestamp milliseconds are out of range") from exc
