from dataclasses import replace
from datetime import timedelta

import pytest

from maais.db.recovery_store import PostgresRecoveryStateStore
from maais.db.unit_of_work import UnitOfWork
from maais.market_data.recovery import RecoveryState, detect_closed_bar_gap, validate_backfill
from maais.orchestration.recovery import GapRecoveryNotCaughtUp, recovery_id_for_gap
from tests.integration.test_operational_state_repository import _manifest_in_database
from tests.unit.market_data.test_frame_builder import NOW
from tests.unit.market_data.test_gap_recovery import _closed_bar, _cursor

pytestmark = pytest.mark.integration


async def test_postgres_recovery_completion_locks_and_verifies_caught_up_cursor(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    initial = replace(_cursor(), experiment_id=manifest.experiment_id)
    candidate = _closed_bar(3, 103)
    gap = detect_closed_bar_gap(initial, candidate)
    assert gap is not None
    batch = validate_backfill(gap, (_closed_bar(1, 101), _closed_bar(2, 102)))
    detected = RecoveryState.create(
        recovery_id=recovery_id_for_gap(gap),
        experiment_id=manifest.experiment_id,
        gap=gap,
        started_at=NOW + timedelta(minutes=5),
    )
    running = detected.begin(NOW + timedelta(minutes=5, milliseconds=1))
    caught_up = initial
    for event in (*batch.events, candidate):
        caught_up = caught_up.advance_closed_bar(event)
        running = running.record_dispatch(
            caught_up,
            NOW + timedelta(minutes=5, milliseconds=running.version),
        )
    completed = running.complete(batch, NOW + timedelta(minutes=6))
    store = PostgresRecoveryStateStore(uow_factory)

    await store.save(detected)
    await store.save(running)
    async with uow_factory.begin() as uow:
        await uow.market_data.record_cursor(caught_up)

    wrong = replace(caught_up, event_id="conflicting-candidate")
    with pytest.raises(GapRecoveryNotCaughtUp, match="differs"):
        await store.complete(completed, expected_cursor=wrong)
    assert await store.load(completed.recovery_id) == running

    await store.complete(completed, expected_cursor=caught_up)

    assert await store.load(completed.recovery_id) == completed


async def test_postgres_store_finds_active_recovery_for_partially_advanced_cursor(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _manifest_in_database(uow_factory)
    initial = replace(_cursor(), experiment_id=manifest.experiment_id)
    candidate = _closed_bar(3, 103)
    gap = detect_closed_bar_gap(initial, candidate)
    assert gap is not None
    running = RecoveryState.create(
        recovery_id=recovery_id_for_gap(gap),
        experiment_id=manifest.experiment_id,
        gap=gap,
        started_at=NOW + timedelta(minutes=5),
    ).begin(NOW + timedelta(minutes=5, milliseconds=1))
    partial_cursor = initial.advance_closed_bar(_closed_bar(1, 101))
    running = running.record_dispatch(
        partial_cursor,
        NOW + timedelta(minutes=5, milliseconds=2),
    )
    store = PostgresRecoveryStateStore(uow_factory)
    await store.save(running)

    assert await store.load_active(partial_cursor) == running
