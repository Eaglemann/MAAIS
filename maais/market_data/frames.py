from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from uuid import NAMESPACE_URL, UUID, uuid5

from maais.domain.json import content_hash
from maais.market_data.events import (
    ClosedBarPayload,
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
    OrderBookPayload,
    PriceLevel,
    ReferenceKind,
    ReferencePricePayload,
    SymbolStatePayload,
    VenueClockPayload,
)


class FrameIdentityConflict(RuntimeError):
    pass


class TimestampBasis(StrEnum):
    VENUE_EVENT = "venue_event"
    LOCAL_OBSERVATION = "local_observation"


@dataclass(frozen=True, slots=True)
class FrameKey:
    experiment_id: UUID
    strategy_version_id: UUID
    symbol: str
    timeframe: str
    bar_close_at: datetime

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or self.strategy_version_id.int == 0:
            raise ValueError("frame key UUIDs cannot be nil")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("frame key symbol must be nonempty uppercase")
        if not self.timeframe:
            raise ValueError("frame key timeframe is required")
        if self.bar_close_at.tzinfo is None or self.bar_close_at.utcoffset() != timedelta(0):
            raise ValueError("frame key bar_close_at must be UTC-aware")

    @property
    def frame_id(self) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            "maais://market-frame/"
            f"{self.experiment_id}/{self.strategy_version_id}/{self.symbol}/"
            f"{self.timeframe}/{self.bar_close_at.isoformat()}",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "strategy_version_id": self.strategy_version_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_close_at": self.bar_close_at,
        }


@dataclass(frozen=True, slots=True)
class SourceObservation:
    venue: str
    stream: str
    event_id: str
    content_hash: str
    venue_event_at: datetime
    observed_at: datetime
    sequence: int | None
    timestamp_basis: TimestampBasis

    @classmethod
    def from_event(cls, event: ObservedMarketEvent) -> SourceObservation:
        timestamp_basis = TimestampBasis.VENUE_EVENT
        if event.venue_event_at == event.observed_at:
            timestamp_basis = TimestampBasis.LOCAL_OBSERVATION
        return cls(
            venue=event.venue,
            stream=event.stream,
            event_id=event.event_id,
            content_hash=event.content_hash,
            venue_event_at=event.venue_event_at,
            observed_at=event.observed_at,
            sequence=event.sequence,
            timestamp_basis=timestamp_basis,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue,
            "stream": self.stream,
            "event_id": self.event_id,
            "content_hash": self.content_hash,
            "venue_event_at": self.venue_event_at,
            "observed_at": self.observed_at,
            "sequence": self.sequence,
            "timestamp_basis": self.timestamp_basis,
        }


@dataclass(frozen=True, slots=True)
class CausalMinuteFrame:
    key: FrameKey
    frame_id: UUID
    cutoff_at: datetime
    bar: ClosedBarPayload
    book_bids: tuple[PriceLevel, ...]
    book_asks: tuple[PriceLevel, ...]
    best_bid: Decimal | None
    best_ask: Decimal | None
    mark_price: Decimal | None
    index_price: Decimal | None
    funding_rate: Decimal | None
    primary_spot_price: Decimal | None
    secondary_venue_price: Decimal | None
    venue_server_time: datetime | None
    symbol_status: str | None
    source_manifest: Mapping[str, SourceObservation]
    content_hash: str

    def __post_init__(self) -> None:
        if self.frame_id != self.key.frame_id:
            raise ValueError("frame_id differs from deterministic frame key")
        if len(self.content_hash) != 64:
            raise ValueError("frame content_hash must be SHA-256")
        if bool(self.book_bids) != bool(self.book_asks):
            raise ValueError("causal frame book sides must be present together")
        if self.book_bids:
            if self.best_bid != self.book_bids[0].price or self.best_ask != self.book_asks[0].price:
                raise ValueError("causal frame best prices differ from captured book depth")
        elif self.best_bid is not None or self.best_ask is not None:
            raise ValueError("causal frame best prices require captured book depth")
        object.__setattr__(self, "source_manifest", MappingProxyType(dict(self.source_manifest)))

    def normalized(self) -> dict[str, object]:
        return {
            "key": self.key.to_dict(),
            "frame_id": self.frame_id,
            "cutoff_at": self.cutoff_at,
            "bar": self.bar.to_dict(),
            "book_bids": [level.to_dict() for level in self.book_bids],
            "book_asks": [level.to_dict() for level in self.book_asks],
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "funding_rate": self.funding_rate,
            "primary_spot_price": self.primary_spot_price,
            "secondary_venue_price": self.secondary_venue_price,
            "venue_server_time": self.venue_server_time,
            "symbol_status": self.symbol_status,
            "source_manifest": {
                name: source.to_dict() for name, source in self.source_manifest.items()
            },
        }


class CausalMinuteFrameBuilder:
    def build(
        self,
        key: FrameKey,
        closed_bar_event: ObservedMarketEvent,
        events: tuple[ObservedMarketEvent, ...],
        *,
        decision_cutoff: datetime | None = None,
    ) -> CausalMinuteFrame:
        if closed_bar_event.kind is not MarketEventKind.CLOSED_BAR or not isinstance(
            closed_bar_event.payload, ClosedBarPayload
        ):
            raise ValueError("frame requires a closed-bar event")
        bar = closed_bar_event.payload
        if not bar.closed:
            raise ValueError("frame requires a fully closed bar")
        if bar.timeframe != "1m" or bar.bar_close_at - bar.bar_open_at != timedelta(minutes=1):
            raise ValueError("frame builder requires an aligned one-minute bar")
        if (
            key.symbol != closed_bar_event.symbol
            or key.timeframe != bar.timeframe
            or key.bar_close_at != bar.bar_close_at
        ):
            raise ValueError("frame key does not match the closed bar")
        cutoff = decision_cutoff or closed_bar_event.observed_at
        if cutoff.tzinfo is None or cutoff.utcoffset() != timedelta(0):
            raise ValueError("frame decision cutoff must be UTC-aware")
        if cutoff < bar.bar_close_at or cutoff > closed_bar_event.observed_at:
            raise ValueError(
                "frame decision cutoff must fall between bar close and bar observation"
            )
        available = tuple(
            event for event in events if event.symbol == key.symbol and event.observed_at <= cutoff
        )
        deduplicated = self._deduplicate((*available, closed_bar_event))
        canonical_bar = next(
            (
                event
                for event in deduplicated
                if event.identity == closed_bar_event.identity
                and event.content_hash == closed_bar_event.content_hash
            ),
            None,
        )
        if canonical_bar is None:
            raise FrameIdentityConflict("closed bar identity has different content")

        book_event = self._latest(
            deduplicated,
            lambda event: (
                event.kind is MarketEventKind.ORDER_BOOK and event.venue == closed_bar_event.venue
            ),
        )
        mark_event = self._latest(
            deduplicated,
            lambda event: (
                event.kind is MarketEventKind.MARK_FUNDING and event.venue == closed_bar_event.venue
            ),
        )
        spot_event = self._latest_reference(deduplicated, ReferenceKind.PRIMARY_SPOT)
        secondary_event = self._latest_reference(deduplicated, ReferenceKind.SECONDARY_VENUE)
        clock_event = self._latest(
            deduplicated,
            lambda event: (
                event.kind is MarketEventKind.VENUE_CLOCK and event.venue == closed_bar_event.venue
            ),
        )
        symbol_event = self._latest(
            deduplicated,
            lambda event: (
                event.kind is MarketEventKind.SYMBOL_STATE and event.venue == closed_bar_event.venue
            ),
        )

        book = self._payload(book_event, OrderBookPayload)
        mark = self._payload(mark_event, MarkFundingPayload)
        spot = self._payload(spot_event, ReferencePricePayload)
        secondary = self._payload(secondary_event, ReferencePricePayload)
        venue_clock = self._payload(clock_event, VenueClockPayload)
        symbol_state = self._payload(symbol_event, SymbolStatePayload)
        selected = {
            "closed_bar": canonical_bar,
            "order_book": book_event,
            "mark_funding": mark_event,
            "primary_spot": spot_event,
            "secondary_venue": secondary_event,
            "venue_clock": clock_event,
            "symbol_state": symbol_event,
        }
        manifest = {
            name: SourceObservation.from_event(event)
            for name, event in selected.items()
            if event is not None
        }
        normalized: dict[str, object] = {
            "key": key.to_dict(),
            "frame_id": key.frame_id,
            "cutoff_at": cutoff,
            "bar": bar.to_dict(),
            "book_bids": [level.to_dict() for level in book.bids] if book else [],
            "book_asks": [level.to_dict() for level in book.asks] if book else [],
            "best_bid": book.best_bid if book else None,
            "best_ask": book.best_ask if book else None,
            "mark_price": mark.mark_price if mark else None,
            "index_price": mark.index_price if mark else None,
            "funding_rate": mark.funding_rate if mark else None,
            "primary_spot_price": spot.price if spot else None,
            "secondary_venue_price": secondary.price if secondary else None,
            "venue_server_time": venue_clock.server_time if venue_clock else None,
            "symbol_status": symbol_state.status if symbol_state else None,
            "source_manifest": {name: source.to_dict() for name, source in manifest.items()},
        }
        return CausalMinuteFrame(
            key=key,
            frame_id=key.frame_id,
            cutoff_at=cutoff,
            bar=bar,
            book_bids=book.bids if book else (),
            book_asks=book.asks if book else (),
            best_bid=book.best_bid if book else None,
            best_ask=book.best_ask if book else None,
            mark_price=mark.mark_price if mark else None,
            index_price=mark.index_price if mark else None,
            funding_rate=mark.funding_rate if mark else None,
            primary_spot_price=spot.price if spot else None,
            secondary_venue_price=secondary.price if secondary else None,
            venue_server_time=venue_clock.server_time if venue_clock else None,
            symbol_status=symbol_state.status if symbol_state else None,
            source_manifest=manifest,
            content_hash=content_hash(normalized),
        )

    @staticmethod
    def _deduplicate(
        events: tuple[ObservedMarketEvent, ...],
    ) -> tuple[ObservedMarketEvent, ...]:
        by_identity: dict[tuple[str, str, str, str], ObservedMarketEvent] = {}
        for event in events:
            existing = by_identity.get(event.identity)
            if existing is None:
                by_identity[event.identity] = event
            elif existing.content_hash != event.content_hash:
                raise FrameIdentityConflict(
                    f"event {event.identity!r} has different content under the same identity"
                )
        return tuple(by_identity.values())

    @staticmethod
    def _latest(
        events: tuple[ObservedMarketEvent, ...],
        predicate: Callable[[ObservedMarketEvent], bool],
    ) -> ObservedMarketEvent | None:
        candidates = (event for event in events if predicate(event))
        return max(
            candidates,
            key=lambda event: (
                event.observed_at,
                event.venue_event_at,
                event.sequence if event.sequence is not None else -1,
                event.event_id,
                event.content_hash,
            ),
            default=None,
        )

    def _latest_reference(
        self,
        events: tuple[ObservedMarketEvent, ...],
        reference_kind: ReferenceKind,
    ) -> ObservedMarketEvent | None:
        return self._latest(
            events,
            lambda event: (
                event.kind is MarketEventKind.REFERENCE_PRICE
                and isinstance(event.payload, ReferencePricePayload)
                and event.payload.reference_kind is reference_kind
            ),
        )

    @staticmethod
    def _payload[T](event: ObservedMarketEvent | None, expected: type[T]) -> T | None:
        if event is None:
            return None
        if not isinstance(event.payload, expected):
            raise TypeError(f"event payload is not {expected.__name__}")
        return event.payload
