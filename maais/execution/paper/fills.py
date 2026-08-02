from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from maais.domain.enums import PaperOrderSide
from maais.execution.paper.clock import require_utc
from maais.execution.paper.market import BookLevel, BookSnapshot, require_positive_decimal


class FillRejection(RuntimeError):
    def __init__(self, reason: str, market_event_id: str | None = None) -> None:
        self.reason = reason
        self.market_event_id = market_event_id
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class MarketFillRequest:
    symbol: str
    side: PaperOrderSide
    quantity: Decimal
    eligible_after: datetime
    decision_executable_price: Decimal
    taker_fee_rate: Decimal

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        require_positive_decimal(self.quantity, "quantity")
        require_positive_decimal(self.decision_executable_price, "decision_executable_price")
        if (
            not isinstance(self.taker_fee_rate, Decimal)
            or not self.taker_fee_rate.is_finite()
            or not Decimal("0") <= self.taker_fee_rate <= Decimal("1")
        ):
            raise ValueError("taker_fee_rate must be a finite Decimal in [0, 1]")
        require_utc(self.eligible_after, "eligible_after")


@dataclass(frozen=True, slots=True)
class FillSlice:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class PaperFill:
    market_event_id: str
    symbol: str
    side: PaperOrderSide
    fill_at: datetime
    quantity: Decimal
    price: Decimal
    notional: Decimal
    slices: tuple[FillSlice, ...]
    liquidity_role: str
    fee: Decimal
    spread_cost: Decimal
    depth_slippage: Decimal
    latency_slippage: Decimal
    total_slippage: Decimal
    book: BookSnapshot


class MarketFillEngine:
    def __init__(self, max_book_age: timedelta) -> None:
        if max_book_age <= timedelta(0):
            raise ValueError("max_book_age must be positive")
        self._max_book_age = max_book_age

    def fill(
        self,
        request: MarketFillRequest,
        books: tuple[BookSnapshot, ...],
    ) -> PaperFill:
        eligible = sorted(
            (
                book
                for book in books
                if book.symbol == request.symbol and book.observed_at > request.eligible_after
            ),
            key=lambda book: (book.observed_at, book.sequence),
        )
        if not eligible:
            raise FillRejection("no_eligible_book")
        book = eligible[0]
        if book.observed_at - book.venue_event_at > self._max_book_age:
            raise FillRejection("stale_book", book.event_id)

        levels = book.asks if request.side is PaperOrderSide.BUY else book.bids
        slices = self._walk(levels, request.quantity, book.event_id)
        filled_notional = sum(
            (item.price * item.quantity for item in slices),
            start=Decimal("0"),
        )
        average_price = filled_notional / request.quantity
        first_price = levels[0].price
        if request.side is PaperOrderSide.BUY:
            spread_cost = (first_price - book.midpoint) * request.quantity
            depth_slippage = (average_price - first_price) * request.quantity
            latency_slippage = (first_price - request.decision_executable_price) * request.quantity
        else:
            spread_cost = (book.midpoint - first_price) * request.quantity
            depth_slippage = (first_price - average_price) * request.quantity
            latency_slippage = (request.decision_executable_price - first_price) * request.quantity
        return PaperFill(
            market_event_id=book.event_id,
            symbol=request.symbol,
            side=request.side,
            fill_at=book.observed_at,
            quantity=request.quantity,
            price=average_price,
            notional=filled_notional,
            slices=slices,
            liquidity_role="taker",
            fee=filled_notional * request.taker_fee_rate,
            spread_cost=spread_cost,
            depth_slippage=depth_slippage,
            latency_slippage=latency_slippage,
            total_slippage=spread_cost + depth_slippage + latency_slippage,
            book=book,
        )

    @staticmethod
    def _walk(
        levels: tuple[BookLevel, ...],
        requested: Decimal,
        market_event_id: str,
    ) -> tuple[FillSlice, ...]:
        remaining = requested
        slices: list[FillSlice] = []
        for level in levels:
            if remaining <= 0:
                break
            quantity = min(level.quantity, remaining)
            slices.append(FillSlice(level.price, quantity))
            remaining -= quantity
        if remaining > 0:
            raise FillRejection("insufficient_visible_depth", market_event_id)
        return tuple(slices)
