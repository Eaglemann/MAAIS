from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest

from maais.market_data.recovery import MarketCursor, RecoveryState, RecoveryStatus
from maais.orchestration.recovery import GapRecoveryManager
from maais.orchestration.worker import (
    ClosedBarDispatchEngine,
    DispatchDisposition,
    WorkerEventConflict,
)
from tests.unit.market_data.test_frame_builder import NOW, _book
from tests.unit.market_data.test_gap_recovery import _closed_bar, _cursor


class _Backfill:
    async def get_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> tuple:
        assert (symbol, interval) == ("BTCUSDT", "1m")
        assert (start, end) == (NOW, NOW + timedelta(minutes=2))
        return _closed_bar(1, 101), _closed_bar(2, 102)


class _Store:
    def __init__(self) -> None:
        self.state: RecoveryState | None = None
        self.completed: RecoveryState | None = None

    async def load(self, recovery_id: UUID) -> RecoveryState | None:
        if self.state is not None and self.state.recovery_id == recovery_id:
            return self.state
        return None

    async def load_active(self, cursor: MarketCursor) -> RecoveryState | None:
        if self.state is not None and self.state.status in {
            RecoveryStatus.DETECTED,
            RecoveryStatus.BACKFILLING,
        }:
            return self.state
        return None

    async def save(self, recovery: RecoveryState) -> None:
        self.state = recovery

    async def complete(
        self,
        recovery: RecoveryState,
        *,
        expected_cursor: MarketCursor,
    ) -> None:
        assert recovery.dispatched_through_sequence == expected_cursor.sequence
        self.state = recovery
        self.completed = recovery


class _Clock:
    def __init__(self) -> None:
        self.value = NOW + timedelta(minutes=10)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(microseconds=1)
        return result


class _Observer:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def observe(self, event, *, context_events) -> None:
        assert context_events[-1].event_id == event.event_id
        self.events.append(event.event_id)


class _Dispatcher:
    def __init__(self, store: _Store, *, fail_once_at: int | None = None) -> None:
        self.store = store
        self.fail_once_at = fail_once_at
        self.failed = False
        self.attempts: list[int] = []
        self.successes: list[int] = []
        self.contexts: list[tuple[str, ...]] = []

    async def dispatch(
        self,
        event,
        *,
        context_events,
        target_cursor,
        recovery_progress,
    ) -> None:
        assert event.sequence is not None
        self.attempts.append(event.sequence)
        self.contexts.append(tuple(item.event_id for item in context_events))
        if self.fail_once_at == event.sequence and not self.failed:
            self.failed = True
            raise RuntimeError("simulated atomic dispatch failure")
        if recovery_progress is not None:
            self.store.state = recovery_progress
        self.successes.append(event.sequence)


def _manager(store: _Store) -> GapRecoveryManager:
    async def no_sleep(_: float) -> None:
        return None

    return GapRecoveryManager(
        backfill=_Backfill(),
        store=store,
        now=_Clock(),
        sleep=no_sleep,
    )


def _engine(
    store: _Store,
    dispatcher: _Dispatcher,
    *,
    observer: _Observer | None = None,
) -> ClosedBarDispatchEngine:
    cursor = _cursor()
    return ClosedBarDispatchEngine(
        experiment_id=cursor.experiment_id,
        symbols=("BTCUSDT",),
        dispatcher=dispatcher,
        recovery=_manager(store),
        cursors={(cursor.venue, cursor.stream, cursor.symbol, cursor.timeframe): cursor},
        continuous=observer,
    )


async def test_nonbar_events_are_observed_once_and_conflicts_fail() -> None:
    store = _Store()
    observer = _Observer()
    engine = _engine(store, _Dispatcher(store), observer=observer)
    book = _book("book-1", 90, "100", "101", 90)

    first = await engine.process(book)
    duplicate = await engine.process(book)

    assert first.disposition is DispatchDisposition.OBSERVED
    assert duplicate.disposition is DispatchDisposition.DUPLICATE
    assert observer.events == ["book-1"]
    with pytest.raises(WorkerEventConflict, match="different content"):
        await engine.process(_book("book-1", 90, "99", "101", 90))


async def test_contiguous_closed_bar_dispatches_and_advances_once() -> None:
    store = _Store()
    dispatcher = _Dispatcher(store)
    engine = _engine(store, dispatcher)

    result = await engine.process(_closed_bar(1, 101))
    duplicate = await engine.process(_closed_bar(1, 101))

    assert result.disposition is DispatchDisposition.DISPATCHED
    assert result.cursor is not None and result.cursor.sequence == 101
    assert duplicate.disposition is DispatchDisposition.DUPLICATE
    assert dispatcher.successes == [101]


async def test_gap_dispatches_every_bar_through_normal_atomic_port_then_completes() -> None:
    store = _Store()
    dispatcher = _Dispatcher(store)
    observer = _Observer()
    engine = _engine(store, dispatcher, observer=observer)

    result = await engine.process(_closed_bar(3, 103))

    assert result.disposition is DispatchDisposition.RECOVERED
    assert result.dispatched_count == 3
    assert result.cursor is not None and result.cursor.sequence == 103
    assert dispatcher.successes == [101, 102, 103]
    assert store.completed is not None
    assert store.completed.status is RecoveryStatus.COMPLETED
    assert store.completed.dispatched_through_sequence == 103
    assert observer.events == ["bar-103", "bar-101", "bar-102"]


async def test_partial_recovery_failure_resumes_without_duplicate_successful_cycles() -> None:
    store = _Store()
    dispatcher = _Dispatcher(store, fail_once_at=102)
    engine = _engine(store, dispatcher)
    candidate = _closed_bar(3, 103)

    with pytest.raises(RuntimeError, match="simulated"):
        await engine.process(candidate)
    assert dispatcher.successes == [101]
    assert store.state is not None and store.state.dispatched_through_sequence == 101

    result = await engine.process(candidate)

    assert result.disposition is DispatchDisposition.RECOVERED
    assert dispatcher.attempts == [101, 102, 102, 103]
    assert dispatcher.successes == [101, 102, 103]
    assert result.cursor is not None and result.cursor.sequence == 103
