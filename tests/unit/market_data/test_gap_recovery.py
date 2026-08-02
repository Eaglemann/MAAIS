from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from maais.market_data.events import ClosedBarPayload, ObservedMarketEvent
from maais.market_data.recovery import (
    BackfillValidationError,
    MarketCursor,
    RecoveryState,
    RecoveryStatus,
    detect_closed_bar_gap,
    validate_backfill,
)
from tests.unit.market_data.test_frame_builder import NOW, _bar


def _closed_bar(minutes_after: int, sequence: int) -> ObservedMarketEvent:
    template = _bar()
    assert isinstance(template.payload, ClosedBarPayload)
    open_at = template.payload.bar_open_at + timedelta(minutes=minutes_after)
    close_at = template.payload.bar_close_at + timedelta(minutes=minutes_after)
    observed_at = template.observed_at + timedelta(minutes=minutes_after)
    return replace(
        template,
        event_id=f"bar-{sequence}",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=sequence,
        payload=replace(
            template.payload,
            bar_open_at=open_at,
            bar_close_at=close_at,
            open=Decimal("100") + minutes_after,
            high=Decimal("102") + minutes_after,
            low=Decimal("99") + minutes_after,
            close=Decimal("101") + minutes_after,
        ),
    )


def _cursor() -> MarketCursor:
    first = _closed_bar(0, 100)
    assert isinstance(first.payload, ClosedBarPayload)
    return MarketCursor.create(
        experiment_id=UUID(int=1),
        venue=first.venue,
        stream=first.stream,
        symbol=first.symbol,
        timeframe=first.payload.timeframe,
        event_id=first.event_id,
        sequence=first.sequence,
        venue_event_at=first.venue_event_at,
        observed_at=first.observed_at,
        bar_close_at=first.payload.bar_close_at,
        updated_at=first.observed_at,
    )


def test_gap_detection_returns_every_exact_missing_bar_open() -> None:
    cursor = _cursor()
    candidate = _closed_bar(3, 103)

    gap = detect_closed_bar_gap(cursor, candidate)

    assert gap is not None
    assert gap.start_open_at == cursor.bar_close_at
    assert gap.end_open_at_exclusive == candidate.payload.bar_open_at  # type: ignore[union-attr]
    assert gap.missing_open_times == (
        NOW,
        NOW + timedelta(minutes=1),
    )
    assert gap.missing_count == 2
    assert gap.missing_sequences == (101, 102)
    with pytest.raises(ValueError, match="time and sequence coverage differ"):
        detect_closed_bar_gap(cursor, _closed_bar(3, 105))


def test_no_gap_advances_cursor_and_regression_is_rejected() -> None:
    cursor = _cursor()
    next_bar = _closed_bar(1, 101)

    assert detect_closed_bar_gap(cursor, next_bar) is None
    advanced = cursor.advance_closed_bar(next_bar)

    assert advanced.sequence == 101
    assert advanced.bar_close_at == NOW + timedelta(minutes=1)
    assert advanced.version == cursor.version + 1
    with pytest.raises(ValueError, match="regression"):
        advanced.advance_closed_bar(_closed_bar(0, 100))


def test_backfill_requires_exact_ordered_closed_coverage() -> None:
    gap = detect_closed_bar_gap(_cursor(), _closed_bar(3, 103))
    assert gap is not None
    recovered = (_closed_bar(1, 101), _closed_bar(2, 102))

    batch = validate_backfill(gap, recovered)

    assert batch.events == recovered
    assert len(batch.content_hash) == 64
    with pytest.raises(BackfillValidationError, match="coverage"):
        validate_backfill(gap, recovered[:1])
    with pytest.raises(BackfillValidationError, match="ordered"):
        validate_backfill(gap, tuple(reversed(recovered)))
    with pytest.raises(BackfillValidationError, match="closed"):
        first = recovered[0]
        assert isinstance(first.payload, ClosedBarPayload)
        validate_backfill(
            gap, (replace(first, payload=replace(first.payload, closed=False)), recovered[1])
        )
    with pytest.raises(BackfillValidationError, match="sequence coverage"):
        validate_backfill(gap, (recovered[0], replace(recovered[1], sequence=105)))


def test_recovery_state_blocks_entries_until_validated_batch_completes() -> None:
    gap = detect_closed_bar_gap(_cursor(), _closed_bar(3, 103))
    assert gap is not None
    batch = validate_backfill(gap, (_closed_bar(1, 101), _closed_bar(2, 102)))
    state = RecoveryState.create(
        recovery_id=UUID(int=10),
        experiment_id=UUID(int=1),
        gap=gap,
        started_at=NOW + timedelta(minutes=3),
    )

    running = state.begin(NOW + timedelta(minutes=3, milliseconds=1))
    completed = running.complete(batch, NOW + timedelta(minutes=3, milliseconds=2))

    assert state.entries_blocked
    assert running.status is RecoveryStatus.BACKFILLING
    assert running.entries_blocked
    assert completed.status is RecoveryStatus.COMPLETED
    assert not completed.entries_blocked
    assert completed.source_hash == batch.content_hash
    assert [event.sequence for event in completed.events] == [1, 2, 3]


def test_failed_recovery_remains_blocking_and_cannot_complete() -> None:
    gap = detect_closed_bar_gap(_cursor(), _closed_bar(3, 103))
    assert gap is not None
    state = RecoveryState.create(
        recovery_id=UUID(int=10),
        experiment_id=UUID(int=1),
        gap=gap,
        started_at=NOW + timedelta(minutes=3),
    ).begin(NOW + timedelta(minutes=3, milliseconds=1))
    failed = state.fail("backfill_timeout", NOW + timedelta(minutes=4))

    assert failed.status is RecoveryStatus.FAILED
    assert failed.entries_blocked
    with pytest.raises(RuntimeError, match="backfilling"):
        failed.complete(
            validate_backfill(gap, (_closed_bar(1, 101), _closed_bar(2, 102))),
            NOW + timedelta(minutes=5),
        )
