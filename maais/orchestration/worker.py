"""Deterministic, exactly-once closed-bar dispatch engine for paper workers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from maais.market_data.events import (
    ClosedBarPayload,
    MarketEventKind,
    ObservedMarketEvent,
)
from maais.market_data.recovery import MarketCursor, RecoveryState
from maais.orchestration.recovery import GapRecoveryManager

CursorKey = tuple[str, str, str, str]
EventIdentity = tuple[str, str, str, str]


class WorkerEventConflict(RuntimeError):
    pass


class DispatchDisposition(StrEnum):
    OBSERVED = "observed"
    DUPLICATE = "duplicate"
    DISPATCHED = "dispatched"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    disposition: DispatchDisposition
    event_id: str
    cursor: MarketCursor | None
    recovery_id: UUID | None
    dispatched_count: int


class CycleDispatchPort(Protocol):
    async def dispatch(
        self,
        event: ObservedMarketEvent,
        *,
        context_events: tuple[ObservedMarketEvent, ...],
        target_cursor: MarketCursor,
        recovery_progress: RecoveryState | None,
    ) -> None:
        """Persist one cycle and cursor, plus recovery progress when supplied, atomically."""


class ContinuousEventPort(Protocol):
    async def observe(
        self,
        event: ObservedMarketEvent,
        *,
        context_events: tuple[ObservedMarketEvent, ...],
    ) -> None: ...


class RecoveryLifecyclePort(Protocol):
    async def recovering(self, recovery: RecoveryState) -> None: ...

    async def recovered(self, recovery: RecoveryState) -> None: ...


class _NoopContinuousObserver:
    async def observe(
        self,
        event: ObservedMarketEvent,
        *,
        context_events: tuple[ObservedMarketEvent, ...],
    ) -> None:
        return None


class _NoopRecoveryLifecycle:
    async def recovering(self, recovery: RecoveryState) -> None:
        return None

    async def recovered(self, recovery: RecoveryState) -> None:
        return None


class MarketEventJournal:
    """Bounded in-memory causal window with conflict-aware event identities."""

    def __init__(self, symbols: Sequence[str], *, capacity_per_symbol: int = 20_000) -> None:
        normalized = tuple(symbols)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("journal symbols must be nonempty and unique")
        if any(not symbol or symbol != symbol.upper() for symbol in normalized):
            raise ValueError("journal symbols must be uppercase")
        if capacity_per_symbol <= 0:
            raise ValueError("journal capacity must be positive")
        self._symbols = frozenset(normalized)
        self._capacity = capacity_per_symbol
        self._events: dict[str, list[ObservedMarketEvent]] = {symbol: [] for symbol in normalized}
        self._hashes: dict[EventIdentity, str] = {}

    def append(self, event: ObservedMarketEvent) -> bool:
        if event.symbol not in self._symbols:
            raise WorkerEventConflict(f"event symbol is not configured: {event.symbol}")
        existing = self._hashes.get(event.identity)
        if existing is not None:
            if existing != event.content_hash:
                raise WorkerEventConflict(
                    f"event identity has different content: {event.identity!r}"
                )
            return False
        values = self._events[event.symbol]
        values.append(event)
        self._hashes[event.identity] = event.content_hash
        if len(values) > self._capacity:
            removed = values.pop(0)
            self._hashes.pop(removed.identity, None)
        return True

    def remove(self, event: ObservedMarketEvent) -> None:
        if self._hashes.get(event.identity) != event.content_hash:
            return
        values = self._events[event.symbol]
        for index in range(len(values) - 1, -1, -1):
            if values[index].identity == event.identity:
                values.pop(index)
                self._hashes.pop(event.identity, None)
                return

    def for_symbol(self, symbol: str) -> tuple[ObservedMarketEvent, ...]:
        try:
            return tuple(self._events[symbol])
        except KeyError as exc:
            raise ValueError(f"journal symbol is not configured: {symbol}") from exc


class ClosedBarDispatchEngine:
    """Serializes public events and routes gaps through the normal cycle path."""

    def __init__(
        self,
        *,
        experiment_id: UUID,
        symbols: Sequence[str],
        dispatcher: CycleDispatchPort,
        recovery: GapRecoveryManager,
        cursors: Mapping[CursorKey, MarketCursor] | None = None,
        continuous: ContinuousEventPort | None = None,
        recovery_lifecycle: RecoveryLifecyclePort | None = None,
        journal_capacity_per_symbol: int = 20_000,
    ) -> None:
        if experiment_id.int == 0:
            raise ValueError("worker experiment_id cannot be nil")
        self._experiment_id = experiment_id
        self._journal = MarketEventJournal(
            symbols,
            capacity_per_symbol=journal_capacity_per_symbol,
        )
        self._dispatcher = dispatcher
        self._recovery = recovery
        self._continuous = continuous or _NoopContinuousObserver()
        self._recovery_lifecycle = recovery_lifecycle or _NoopRecoveryLifecycle()
        self._cursors = dict(cursors or {})
        for key, cursor in self._cursors.items():
            if cursor.experiment_id != experiment_id or key != _cursor_key(cursor):
                raise ValueError("restored cursor identity differs from worker configuration")
        self._lock = asyncio.Lock()

    @property
    def cursors(self) -> Mapping[CursorKey, MarketCursor]:
        return dict(self._cursors)

    @property
    def journal(self) -> MarketEventJournal:
        return self._journal

    async def process(self, event: ObservedMarketEvent) -> DispatchResult:
        async with self._lock:
            appended: list[ObservedMarketEvent] = []
            try:
                if not self._journal.append(event):
                    return DispatchResult(
                        DispatchDisposition.DUPLICATE,
                        event.event_id,
                        self._cursor_for(event),
                        None,
                        0,
                    )
                appended.append(event)
                await self._continuous.observe(
                    event,
                    context_events=self._journal.for_symbol(event.symbol),
                )
                if event.kind is not MarketEventKind.CLOSED_BAR:
                    return DispatchResult(
                        DispatchDisposition.OBSERVED,
                        event.event_id,
                        None,
                        None,
                        0,
                    )
                return await self._dispatch_closed_bar(event, appended)
            except BaseException:
                for item in reversed(appended):
                    self._journal.remove(item)
                raise

    async def _dispatch_closed_bar(
        self,
        candidate: ObservedMarketEvent,
        appended: list[ObservedMarketEvent],
    ) -> DispatchResult:
        payload = candidate.payload
        if not isinstance(payload, ClosedBarPayload) or not payload.closed:
            raise WorkerEventConflict("closed-bar dispatch requires a final bar payload")
        key = _event_cursor_key(candidate)
        cursor = self._cursors.get(key)
        if cursor is None:
            target = MarketCursor.create(
                experiment_id=self._experiment_id,
                venue=candidate.venue,
                stream=candidate.stream,
                symbol=candidate.symbol,
                timeframe=payload.timeframe,
                event_id=candidate.event_id,
                sequence=candidate.sequence,
                venue_event_at=candidate.venue_event_at,
                observed_at=candidate.observed_at,
                bar_close_at=payload.bar_close_at,
                updated_at=candidate.observed_at,
            )
            await self._dispatcher.dispatch(
                candidate,
                context_events=self._journal.for_symbol(candidate.symbol),
                target_cursor=target,
                recovery_progress=None,
            )
            self._cursors[key] = target
            return DispatchResult(
                DispatchDisposition.DISPATCHED,
                candidate.event_id,
                target,
                None,
                1,
            )

        preparation = await self._recovery.prepare(cursor, candidate)
        if preparation is None:
            target = cursor.advance_closed_bar(candidate)
            await self._dispatcher.dispatch(
                candidate,
                context_events=self._journal.for_symbol(candidate.symbol),
                target_cursor=target,
                recovery_progress=None,
            )
            self._cursors[key] = target
            return DispatchResult(
                DispatchDisposition.DISPATCHED,
                candidate.event_id,
                target,
                None,
                1,
            )

        await self._recovery_lifecycle.recovering(preparation.recovery)
        dispatched = 0
        for recovered_event in preparation.dispatch_events:
            if recovered_event.identity != candidate.identity:
                if self._journal.append(recovered_event):
                    appended.append(recovered_event)
                    await self._continuous.observe(
                        recovered_event,
                        context_events=self._journal.for_symbol(recovered_event.symbol),
                    )
            target = cursor.advance_closed_bar(recovered_event)
            progressed = self._recovery.progress(
                preparation,
                dispatched_cursor=target,
            )
            await self._dispatcher.dispatch(
                recovered_event,
                context_events=self._journal.for_symbol(recovered_event.symbol),
                target_cursor=target,
                recovery_progress=progressed.recovery,
            )
            preparation = progressed
            cursor = target
            self._cursors[key] = cursor
            dispatched += 1
        completed = await self._recovery.complete(
            preparation,
            caught_up_cursor=cursor,
        )
        await self._recovery_lifecycle.recovered(completed)
        return DispatchResult(
            DispatchDisposition.RECOVERED,
            candidate.event_id,
            cursor,
            completed.recovery_id,
            dispatched,
        )

    def _cursor_for(self, event: ObservedMarketEvent) -> MarketCursor | None:
        if not isinstance(event.payload, ClosedBarPayload):
            return None
        return self._cursors.get(_event_cursor_key(event))


def _event_cursor_key(event: ObservedMarketEvent) -> CursorKey:
    payload = event.payload
    if not isinstance(payload, ClosedBarPayload):
        raise ValueError("cursor keys require closed bars")
    return event.venue, event.stream, event.symbol, payload.timeframe


def _cursor_key(cursor: MarketCursor) -> CursorKey:
    return cursor.venue, cursor.stream, cursor.symbol, cursor.timeframe
