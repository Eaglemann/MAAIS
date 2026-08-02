from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from maais.market_data.recovery import (
    MarketCursor,
    RecoveryState,
    RecoveryStatus,
    detect_closed_bar_gap,
    validate_backfill,
)
from maais.orchestration.recovery import (
    GapRecoveryFailed,
    GapRecoveryManager,
    GapRecoveryNotCaughtUp,
    RecoveryPreparation,
    recovery_id_for_gap,
)
from tests.unit.market_data.test_frame_builder import NOW
from tests.unit.market_data.test_gap_recovery import _closed_bar, _cursor


class _Clock:
    def __init__(self) -> None:
        self.current = NOW + timedelta(minutes=10)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=1)
        return value


class _Backfill:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    async def get_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> tuple:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("public REST timeout")
        assert (symbol, interval) == ("BTCUSDT", "1m")
        assert (start, end) == (NOW, NOW + timedelta(minutes=2))
        return (_closed_bar(1, 101), _closed_bar(2, 102))


class _Store:
    def __init__(self, existing: RecoveryState | None = None) -> None:
        self.existing = existing
        self.saved: list[RecoveryState] = []
        self.completed: tuple[RecoveryState, MarketCursor] | None = None

    async def load(self, recovery_id: UUID) -> RecoveryState | None:
        if self.existing is not None:
            assert self.existing.recovery_id == recovery_id
        return self.existing

    async def save(self, recovery: RecoveryState) -> None:
        self.saved.append(recovery)
        self.existing = recovery

    async def complete(
        self,
        recovery: RecoveryState,
        *,
        expected_cursor: MarketCursor,
    ) -> None:
        self.completed = (recovery, expected_cursor)
        self.existing = recovery


async def _no_sleep(_: float) -> None:
    return None


def _caught_up(preparation: RecoveryPreparation) -> MarketCursor:
    cursor = _cursor()
    for event in preparation.dispatch_events:
        cursor = cursor.advance_closed_bar(event)
    return cursor


async def test_prepare_persists_detection_before_fetch_and_returns_exact_dispatch_order() -> None:
    store = _Store()
    backfill = _Backfill()
    manager = GapRecoveryManager(
        backfill=backfill,
        store=store,
        now=_Clock(),
        sleep=_no_sleep,
    )

    preparation = await manager.prepare(_cursor(), _closed_bar(3, 103))

    assert preparation is not None
    assert backfill.calls == 1
    assert [state.status for state in store.saved] == [
        RecoveryStatus.DETECTED,
        RecoveryStatus.BACKFILLING,
    ]
    assert [event.sequence for event in preparation.dispatch_events] == [101, 102, 103]
    assert preparation.recovery.recovery_id == recovery_id_for_gap(preparation.batch.gap)


async def test_complete_requires_durable_cursor_at_candidate() -> None:
    store = _Store()
    manager = GapRecoveryManager(
        backfill=_Backfill(),
        store=store,
        now=_Clock(),
        sleep=_no_sleep,
    )
    preparation = await manager.prepare(_cursor(), _closed_bar(3, 103))
    assert preparation is not None

    with pytest.raises(GapRecoveryNotCaughtUp):
        await manager.complete(preparation, caught_up_cursor=_cursor())

    caught_up = _caught_up(preparation)
    completed = await manager.complete(preparation, caught_up_cursor=caught_up)

    assert completed.status is RecoveryStatus.COMPLETED
    assert not completed.entries_blocked
    assert store.completed == (completed, caught_up)


async def test_transient_backfill_failures_are_visible_and_bounded() -> None:
    store = _Store()
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    manager = GapRecoveryManager(
        backfill=_Backfill(failures=2),
        store=store,
        now=_Clock(),
        sleep=sleep,
        max_attempts=3,
        initial_backoff_seconds=0.1,
    )

    preparation = await manager.prepare(_cursor(), _closed_bar(3, 103))

    assert preparation is not None
    assert preparation.recovery.attempt == 3
    assert delays == pytest.approx([0.1, 0.2])
    assert [state.status for state in store.saved] == [
        RecoveryStatus.DETECTED,
        RecoveryStatus.BACKFILLING,
        RecoveryStatus.DETECTED,
        RecoveryStatus.BACKFILLING,
        RecoveryStatus.DETECTED,
        RecoveryStatus.BACKFILLING,
    ]
    assert [
        state.events[-1].event_type
        for state in store.saved
        if state.status is RecoveryStatus.DETECTED
    ] == [
        "market_recovery.detected",
        "market_recovery.retry_scheduled",
        "market_recovery.retry_scheduled",
    ]


async def test_exhausted_backfill_persists_terminal_failure() -> None:
    store = _Store()
    manager = GapRecoveryManager(
        backfill=_Backfill(failures=5),
        store=store,
        now=_Clock(),
        sleep=_no_sleep,
        max_attempts=2,
        initial_backoff_seconds=0,
    )

    with pytest.raises(GapRecoveryFailed) as caught:
        await manager.prepare(_cursor(), _closed_bar(3, 103))

    assert caught.value.recovery.status is RecoveryStatus.FAILED
    assert caught.value.recovery.attempt == 2
    assert caught.value.recovery.failure_reason == "TimeoutError:public REST timeout"
    assert store.saved[-1] == caught.value.recovery


async def test_backfilling_recovery_resumes_without_duplicate_attempt_transition() -> None:
    cursor = _cursor()
    candidate = _closed_bar(3, 103)
    detected_gap = detect_closed_bar_gap(cursor, candidate)
    assert detected_gap is not None
    existing = RecoveryState.create(
        recovery_id=recovery_id_for_gap(detected_gap),
        experiment_id=cursor.experiment_id,
        gap=detected_gap,
        started_at=NOW + timedelta(minutes=5),
    ).begin(NOW + timedelta(minutes=5, milliseconds=1))
    store = _Store(existing)
    manager = GapRecoveryManager(
        backfill=_Backfill(),
        store=store,
        now=_Clock(),
        sleep=_no_sleep,
    )

    preparation = await manager.prepare(cursor, candidate)

    assert preparation is not None
    assert preparation.recovery == existing
    assert store.saved == []


async def test_contiguous_candidate_bypasses_recovery_store() -> None:
    store = _Store()
    manager = GapRecoveryManager(
        backfill=_Backfill(),
        store=store,
        now=_Clock(),
        sleep=_no_sleep,
    )

    preparation = await manager.prepare(_cursor(), _closed_bar(1, 101))

    assert preparation is None
    assert store.saved == []


async def test_completion_rejects_same_sequence_with_different_event_identity() -> None:
    # Guard against declaring recovery complete from a cursor that merely shares
    # the target sequence but was produced by conflicting content.
    cursor = _cursor()
    candidate = _closed_bar(3, 103)
    detected_gap = detect_closed_bar_gap(cursor, candidate)
    assert detected_gap is not None
    batch = validate_backfill(detected_gap, (_closed_bar(1, 101), _closed_bar(2, 102)))
    state = RecoveryState.create(
        recovery_id=recovery_id_for_gap(detected_gap),
        experiment_id=cursor.experiment_id,
        gap=detected_gap,
        started_at=NOW + timedelta(minutes=5),
    ).begin(NOW + timedelta(minutes=5, milliseconds=1))
    preparation = RecoveryPreparation(state, batch, candidate)
    wrong = replace(_caught_up(preparation), event_id="conflicting-candidate")
    manager = GapRecoveryManager(
        backfill=_Backfill(),
        store=_Store(state),
        now=_Clock(),
        sleep=_no_sleep,
    )

    with pytest.raises(GapRecoveryNotCaughtUp):
        await manager.complete(preparation, caught_up_cursor=wrong)
