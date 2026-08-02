from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from maais.domain.json import content_hash


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


def _require_decimal(
    value: Decimal,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and value < 0:
        raise ValueError(f"{field} must be nonnegative")


class MarketEventKind(StrEnum):
    CLOSED_BAR = "closed_bar"
    ORDER_BOOK = "order_book"
    TRADE = "trade"
    MARK_FUNDING = "mark_funding"
    FUNDING_SETTLEMENT = "funding_settlement"
    REFERENCE_PRICE = "reference_price"
    VENUE_CLOCK = "venue_clock"
    SYMBOL_STATE = "symbol_state"


class ReferenceKind(StrEnum):
    PRIMARY_SPOT = "primary_spot"
    SECONDARY_VENUE = "secondary_venue"


class AggressorSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        _require_decimal(self.price, "price level price", positive=True)
        _require_decimal(self.quantity, "price level quantity", positive=True)

    def to_dict(self) -> dict[str, object]:
        return {"price": self.price, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class ClosedBarPayload:
    timeframe: str
    bar_open_at: datetime
    bar_close_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_volume: Decimal
    taker_buy_quote_volume: Decimal
    closed: bool

    def __post_init__(self) -> None:
        if not self.timeframe:
            raise ValueError("bar timeframe is required")
        _require_utc(self.bar_open_at, "bar_open_at")
        _require_utc(self.bar_close_at, "bar_close_at")
        if self.bar_close_at <= self.bar_open_at:
            raise ValueError("bar_close_at must follow bar_open_at")
        for field in ("open", "high", "low", "close"):
            _require_decimal(getattr(self, field), field, positive=True)
        for field in ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"):
            _require_decimal(getattr(self, field), field, nonnegative=True)
        if self.trade_count < 0:
            raise ValueError("trade_count must be nonnegative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("bar OHLC values are inconsistent")
        if self.high < self.low:
            raise ValueError("bar high cannot be below low")
        if self.taker_buy_volume > self.volume:
            raise ValueError("taker buy volume cannot exceed total volume")
        if self.taker_buy_quote_volume > self.quote_volume:
            raise ValueError("taker buy quote volume cannot exceed total quote volume")

    def to_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "bar_open_at": self.bar_open_at,
            "bar_close_at": self.bar_close_at,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trade_count": self.trade_count,
            "taker_buy_volume": self.taker_buy_volume,
            "taker_buy_quote_volume": self.taker_buy_quote_volume,
            "closed": self.closed,
        }


@dataclass(frozen=True, slots=True)
class OrderBookPayload:
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]
    published_at: datetime
    sequence_start: int
    previous_sequence: int | None
    snapshot_sequence: int | None

    def __post_init__(self) -> None:
        _require_utc(self.published_at, "order book published_at")
        if self.sequence_start < 0:
            raise ValueError("order book sequence_start must be nonnegative")
        for value, field in (
            (self.previous_sequence, "previous_sequence"),
            (self.snapshot_sequence, "snapshot_sequence"),
        ):
            if value is not None and value < 0:
                raise ValueError(f"order book {field} must be nonnegative")
        if not self.bids or not self.asks:
            raise ValueError("order book requires bid and ask depth")
        if any(left.price <= right.price for left, right in zip(self.bids, self.bids[1:])):
            raise ValueError("order book bids must be strictly descending")
        if any(left.price >= right.price for left, right in zip(self.asks, self.asks[1:])):
            raise ValueError("order book asks must be strictly ascending")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("order book is crossed or locked")

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    def to_dict(self) -> dict[str, object]:
        return {
            "bids": [item.to_dict() for item in self.bids],
            "asks": [item.to_dict() for item in self.asks],
            "published_at": self.published_at,
            "sequence_start": self.sequence_start,
            "previous_sequence": self.previous_sequence,
            "snapshot_sequence": self.snapshot_sequence,
        }


@dataclass(frozen=True, slots=True)
class TradePayload:
    price: Decimal
    quantity: Decimal
    aggressor_side: AggressorSide

    def __post_init__(self) -> None:
        _require_decimal(self.price, "trade price", positive=True)
        _require_decimal(self.quantity, "trade quantity", positive=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "price": self.price,
            "quantity": self.quantity,
            "aggressor_side": self.aggressor_side,
        }


@dataclass(frozen=True, slots=True)
class MarkFundingPayload:
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal
    next_funding_at: datetime
    estimated_settle_price: Decimal | None

    def __post_init__(self) -> None:
        _require_decimal(self.mark_price, "mark_price", positive=True)
        _require_decimal(self.index_price, "index_price", positive=True)
        _require_decimal(self.funding_rate, "funding_rate")
        _require_utc(self.next_funding_at, "next_funding_at")
        if self.estimated_settle_price is not None:
            _require_decimal(
                self.estimated_settle_price,
                "estimated_settle_price",
                nonnegative=True,
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "funding_rate": self.funding_rate,
            "next_funding_at": self.next_funding_at,
            "estimated_settle_price": self.estimated_settle_price,
        }


@dataclass(frozen=True, slots=True)
class FundingSettlementPayload:
    funding_at: datetime
    funding_rate: Decimal
    mark_price: Decimal
    rate_type: str

    def __post_init__(self) -> None:
        _require_utc(self.funding_at, "funding_at")
        _require_decimal(self.funding_rate, "funding_rate")
        _require_decimal(self.mark_price, "funding mark_price", positive=True)
        if self.rate_type not in {"Regular", "Special"}:
            raise ValueError("funding rate_type must be Regular or Special")

    def to_dict(self) -> dict[str, object]:
        return {
            "funding_at": self.funding_at,
            "funding_rate": self.funding_rate,
            "mark_price": self.mark_price,
            "rate_type": self.rate_type,
        }


@dataclass(frozen=True, slots=True)
class ReferencePricePayload:
    reference_kind: ReferenceKind
    instrument: str
    price: Decimal
    source_event_id: str
    source_quantity: Decimal | None
    source_side: AggressorSide | None
    source_bid: Decimal | None
    source_ask: Decimal | None
    source_published_at: datetime | None

    def __post_init__(self) -> None:
        if not self.instrument or not self.source_event_id:
            raise ValueError("reference instrument and source event are required")
        _require_decimal(self.price, "reference price", positive=True)
        if self.source_quantity is not None:
            _require_decimal(self.source_quantity, "reference source quantity", positive=True)
        if (self.source_bid is None) != (self.source_ask is None):
            raise ValueError("reference bid and ask must be present together")
        if self.source_bid is not None and self.source_ask is not None:
            _require_decimal(self.source_bid, "reference source bid", positive=True)
            _require_decimal(self.source_ask, "reference source ask", positive=True)
            if self.source_bid >= self.source_ask:
                raise ValueError("reference source bid must be below ask")
        if self.source_published_at is not None:
            _require_utc(self.source_published_at, "reference source_published_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "reference_kind": self.reference_kind,
            "instrument": self.instrument,
            "price": self.price,
            "source_event_id": self.source_event_id,
            "source_quantity": self.source_quantity,
            "source_side": self.source_side,
            "source_bid": self.source_bid,
            "source_ask": self.source_ask,
            "source_published_at": self.source_published_at,
        }


@dataclass(frozen=True, slots=True)
class VenueClockPayload:
    server_time: datetime

    def __post_init__(self) -> None:
        _require_utc(self.server_time, "server_time")

    def to_dict(self) -> dict[str, object]:
        return {"server_time": self.server_time}


@dataclass(frozen=True, slots=True)
class SymbolStatePayload:
    status: str

    def __post_init__(self) -> None:
        if not self.status or self.status != self.status.upper():
            raise ValueError("symbol status must be a nonempty uppercase value")

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status}


MarketPayload = (
    ClosedBarPayload
    | OrderBookPayload
    | TradePayload
    | MarkFundingPayload
    | FundingSettlementPayload
    | ReferencePricePayload
    | VenueClockPayload
    | SymbolStatePayload
)

_PAYLOAD_TYPES: dict[MarketEventKind, type[object]] = {
    MarketEventKind.CLOSED_BAR: ClosedBarPayload,
    MarketEventKind.ORDER_BOOK: OrderBookPayload,
    MarketEventKind.TRADE: TradePayload,
    MarketEventKind.MARK_FUNDING: MarkFundingPayload,
    MarketEventKind.FUNDING_SETTLEMENT: FundingSettlementPayload,
    MarketEventKind.REFERENCE_PRICE: ReferencePricePayload,
    MarketEventKind.VENUE_CLOCK: VenueClockPayload,
    MarketEventKind.SYMBOL_STATE: SymbolStatePayload,
}


@dataclass(frozen=True, slots=True)
class ObservedMarketEvent:
    venue: str
    stream: str
    symbol: str
    event_id: str
    kind: MarketEventKind
    venue_event_at: datetime
    observed_at: datetime
    sequence: int | None
    sequence_not_applicable_reason: str | None
    payload: MarketPayload

    def __post_init__(self) -> None:
        if not self.venue or not self.stream or not self.event_id:
            raise ValueError("venue, stream, and event_id are required")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("event symbol must be nonempty uppercase")
        _require_utc(self.venue_event_at, "venue_event_at")
        _require_utc(self.observed_at, "observed_at")
        if self.sequence is None:
            if not self.sequence_not_applicable_reason:
                raise ValueError("missing sequence requires an explicit reason")
        else:
            if self.sequence < 0:
                raise ValueError("sequence must be nonnegative")
            if self.sequence_not_applicable_reason is not None:
                raise ValueError("sequence reason is invalid when a sequence exists")
        expected = _PAYLOAD_TYPES[self.kind]
        if not isinstance(self.payload, expected):
            raise ValueError(
                f"event payload does not match kind {self.kind.value}: expected {expected.__name__}"
            )

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.venue, self.stream, self.symbol, self.event_id)

    @property
    def sequence_scope(self) -> tuple[str, str, str]:
        return (self.venue, self.stream, self.symbol)

    def to_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue,
            "stream": self.stream,
            "symbol": self.symbol,
            "event_id": self.event_id,
            "kind": self.kind,
            "venue_event_at": self.venue_event_at,
            "observed_at": self.observed_at,
            "sequence": self.sequence,
            "sequence_not_applicable_reason": self.sequence_not_applicable_reason,
            "payload": self.payload.to_dict(),
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())
