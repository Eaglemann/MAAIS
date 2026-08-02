"""Live observation buffers used by entry, protection, and benchmark contexts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from maais.execution.paper.clock import require_utc
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.market_data.events import (
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
    OrderBookPayload,
    ReferenceKind,
    ReferencePricePayload,
)
from maais.monitoring.admission import HealthObservation


class RuntimeObservationConflict(RuntimeError):
    pass


class EligibleBookTimeout(TimeoutError):
    pass


class RuntimeHealthRegistry:
    """Explicit event-time health; absent components remain visibly absent."""

    def __init__(self, components: Sequence[str]) -> None:
        normalized = tuple(components)
        if (
            not normalized
            or len(set(normalized)) != len(normalized)
            or any(not item for item in normalized)
        ):
            raise ValueError("runtime health components must be nonempty and unique")
        self._components = frozenset(normalized)
        self._observations: dict[str, HealthObservation] = {}

    def heartbeat(self, component: str, observed_at: datetime) -> None:
        self._record(HealthObservation(component, True, observed_at, None))

    def failure(self, component: str, error: str, observed_at: datetime) -> None:
        if not error:
            raise ValueError("runtime health failure requires an error")
        self._record(HealthObservation(component, False, observed_at, error))

    def snapshot(self) -> tuple[HealthObservation, ...]:
        return tuple(self._observations[name] for name in sorted(self._observations))

    def _record(self, observation: HealthObservation) -> None:
        if observation.component not in self._components:
            raise ValueError(f"unknown runtime health component: {observation.component}")
        previous = self._observations.get(observation.component)
        if previous is not None and observation.observed_at < previous.observed_at:
            raise RuntimeObservationConflict("runtime health observation regressed")
        self._observations[observation.component] = observation


class MarketObservationBuffer:
    """Bound future books and latest causal reference/mark observations."""

    def __init__(self, symbols: Sequence[str], *, capacity_per_symbol: int = 2_000) -> None:
        normalized = tuple(symbols)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("observation symbols must be nonempty and unique")
        if any(not symbol or symbol != symbol.upper() for symbol in normalized):
            raise ValueError("observation symbols must be uppercase")
        if capacity_per_symbol <= 0:
            raise ValueError("observation capacity must be positive")
        self._symbols = frozenset(normalized)
        self._capacity = capacity_per_symbol
        self._marks: dict[str, ObservedMarketEvent] = {}
        self._primary: dict[str, ObservedMarketEvent] = {}
        self._books: dict[str, list[BookSnapshot]] = {symbol: [] for symbol in normalized}
        self._book_hashes: dict[tuple[str, str, str, str], str] = {}
        self._condition = asyncio.Condition()

    async def observe(self, event: ObservedMarketEvent) -> bool:
        if event.symbol not in self._symbols:
            raise ValueError(f"observation symbol is not configured: {event.symbol}")
        if event.kind is MarketEventKind.MARK_FUNDING:
            return self._replace_latest(self._marks, event)
        if event.kind is MarketEventKind.REFERENCE_PRICE:
            payload = event.payload
            if not isinstance(payload, ReferencePricePayload):
                raise TypeError("reference event payload is invalid")
            if payload.reference_kind is ReferenceKind.PRIMARY_SPOT:
                return self._replace_latest(self._primary, event)
            return False
        if event.kind is not MarketEventKind.ORDER_BOOK:
            return False
        payload = event.payload
        if not isinstance(payload, OrderBookPayload):
            raise TypeError("order-book event payload is invalid")
        if event.sequence is None:
            raise RuntimeObservationConflict("paper execution book requires a source sequence")
        existing = self._book_hashes.get(event.identity)
        if existing is not None:
            if existing != event.content_hash:
                raise RuntimeObservationConflict(
                    f"book identity has different content: {event.identity!r}"
                )
            return False
        mark_event = self._marks.get(event.symbol)
        if mark_event is None or mark_event.observed_at > event.observed_at:
            return False
        mark = mark_event.payload
        if not isinstance(mark, MarkFundingPayload):
            raise TypeError("mark event payload is invalid")
        snapshot = BookSnapshot(
            event_id=event.event_id,
            symbol=event.symbol,
            venue_event_at=event.venue_event_at,
            observed_at=event.observed_at,
            sequence=event.sequence,
            bids=tuple(BookLevel(level.price, level.quantity) for level in payload.bids),
            asks=tuple(BookLevel(level.price, level.quantity) for level in payload.asks),
            mark_price=mark.mark_price,
        )
        async with self._condition:
            values = self._books[event.symbol]
            values.append(snapshot)
            self._book_hashes[event.identity] = event.content_hash
            if len(values) > self._capacity:
                removed = values.pop(0)
                stale = next(
                    (identity for identity in self._book_hashes if identity[3] == removed.event_id),
                    None,
                )
                if stale is not None:
                    self._book_hashes.pop(stale, None)
            self._condition.notify_all()
        return True

    async def books_after(
        self,
        symbol: str,
        eligible_after: datetime,
        *,
        timeout: timedelta,
    ) -> tuple[BookSnapshot, ...]:
        self._require_symbol(symbol)
        require_utc(eligible_after, "eligible_after")
        if timeout <= timedelta(0):
            raise ValueError("eligible book timeout must be positive")

        def eligible() -> tuple[BookSnapshot, ...]:
            return tuple(item for item in self._books[symbol] if item.observed_at > eligible_after)

        try:
            async with asyncio.timeout(timeout.total_seconds()):
                async with self._condition:
                    while not (result := eligible()):
                        await self._condition.wait()
                    return result
        except TimeoutError as exc:
            raise EligibleBookTimeout(
                f"no eligible {symbol} book observed after {eligible_after.isoformat()}"
            ) from exc

    def books_at_or_before(
        self,
        symbol: str,
        observed_at: datetime,
    ) -> tuple[BookSnapshot, ...]:
        self._require_symbol(symbol)
        require_utc(observed_at, "book causal cutoff")
        return tuple(item for item in self._books[symbol] if item.observed_at <= observed_at)

    def latest_primary_reference(
        self,
        symbol: str,
        *,
        at_or_before: datetime,
    ) -> ObservedMarketEvent | None:
        self._require_symbol(symbol)
        require_utc(at_or_before, "at_or_before")
        event = self._primary.get(symbol)
        if event is None or event.observed_at > at_or_before:
            return None
        return event

    def latest_mark(
        self,
        symbol: str,
        *,
        at_or_before: datetime,
    ) -> tuple[Decimal, ObservedMarketEvent] | None:
        self._require_symbol(symbol)
        require_utc(at_or_before, "at_or_before")
        event = self._marks.get(symbol)
        if event is None or event.observed_at > at_or_before:
            return None
        payload = event.payload
        if not isinstance(payload, MarkFundingPayload):
            raise TypeError("mark event payload is invalid")
        return payload.mark_price, event

    def _require_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            raise ValueError(f"observation symbol is not configured: {symbol}")

    @staticmethod
    def _replace_latest(
        values: dict[str, ObservedMarketEvent],
        event: ObservedMarketEvent,
    ) -> bool:
        previous = values.get(event.symbol)
        if previous is not None:
            if previous.identity == event.identity:
                if previous.content_hash != event.content_hash:
                    raise RuntimeObservationConflict(
                        f"observation identity has different content: {event.identity!r}"
                    )
                return False
            if event.observed_at < previous.observed_at:
                raise RuntimeObservationConflict("latest market observation regressed")
        values[event.symbol] = event
        return True
