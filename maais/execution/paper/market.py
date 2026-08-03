from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from maais.execution.paper.clock import require_utc


def require_positive_decimal(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a positive finite Decimal")


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        require_positive_decimal(self.price, "level price")
        require_positive_decimal(self.quantity, "level quantity")


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    event_id: str
    symbol: str
    venue_event_at: datetime
    observed_at: datetime
    sequence: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    mark_price: Decimal

    def __post_init__(self) -> None:
        if not self.event_id or not self.symbol:
            raise ValueError("event_id and symbol are required")
        require_utc(self.venue_event_at, "venue_event_at")
        require_utc(self.observed_at, "observed_at")
        if self.venue_event_at > self.observed_at:
            raise ValueError("venue_event_at cannot follow observed_at")
        if self.sequence < 0:
            raise ValueError("sequence must be nonnegative")
        if not self.bids or not self.asks:
            raise ValueError("both bid and ask depth are required")
        bid_prices = [level.price for level in self.bids]
        ask_prices = [level.price for level in self.asks]
        if any(left <= right for left, right in zip(bid_prices, bid_prices[1:])):
            raise ValueError("bids must be strictly descending")
        if any(left >= right for left, right in zip(ask_prices, ask_prices[1:])):
            raise ValueError("asks must be strictly ascending")
        if self.best_bid >= self.best_ask:
            raise ValueError("book must not be crossed or locked")
        require_positive_decimal(self.mark_price, "mark_price")

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def midpoint(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")


@dataclass(frozen=True, slots=True)
class TradePrint:
    event_id: str
    symbol: str
    venue_event_at: datetime
    observed_at: datetime
    sequence: int
    price: Decimal
    quantity: Decimal
    aggressor_side: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.symbol:
            raise ValueError("event_id and symbol are required")
        require_utc(self.venue_event_at, "venue_event_at")
        require_utc(self.observed_at, "observed_at")
        if self.venue_event_at > self.observed_at:
            raise ValueError("venue_event_at cannot follow observed_at")
        if self.sequence < 0:
            raise ValueError("sequence must be nonnegative")
        require_positive_decimal(self.price, "trade price")
        require_positive_decimal(self.quantity, "trade quantity")
        if self.aggressor_side not in {"buy", "sell"}:
            raise ValueError("aggressor_side must be buy or sell")
