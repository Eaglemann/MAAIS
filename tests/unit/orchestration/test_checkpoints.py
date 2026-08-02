from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.orchestration.checkpoints import WorkerCheckpoint, WorkerStatus

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _checkpoint() -> WorkerCheckpoint:
    return WorkerCheckpoint.create(
        experiment_id=UUID(int=1),
        worker_id=UUID(int=2),
        checkpoint_at=NOW,
        state={"cursor_count": 0, "pending_orders": []},
    )


def test_checkpoint_legal_lifecycle_is_versioned_and_immutable() -> None:
    starting = _checkpoint()
    running = starting.transition(
        WorkerStatus.RUNNING,
        NOW + timedelta(seconds=1),
        {"cursor_count": 1, "pending_orders": []},
    )
    recovering = running.transition(
        WorkerStatus.RECOVERING,
        NOW + timedelta(seconds=2),
        {"cursor_count": 1, "recovery_id": str(UUID(int=3))},
    )
    resumed = recovering.transition(
        WorkerStatus.RUNNING,
        NOW + timedelta(seconds=3),
        {"cursor_count": 1, "pending_orders": []},
    )
    stopping = resumed.transition(
        WorkerStatus.STOPPING,
        NOW + timedelta(seconds=4),
        {"queue_depth": 0},
    )
    stopped = stopping.transition(
        WorkerStatus.STOPPED,
        NOW + timedelta(seconds=5),
        {"queue_depth": 0},
    )

    assert stopped.version == 6
    assert [event.sequence for event in stopped.events] == list(range(1, 7))
    with pytest.raises(TypeError):
        stopped.state["queue_depth"] = 2  # type: ignore[index]


def test_checkpoint_rejects_illegal_transition_time_regression_and_terminal_change() -> None:
    checkpoint = _checkpoint()
    with pytest.raises(RuntimeError, match="illegal worker transition"):
        checkpoint.transition(WorkerStatus.STOPPED, NOW + timedelta(seconds=1), {})
    running = checkpoint.transition(WorkerStatus.RUNNING, NOW + timedelta(seconds=1), {})
    with pytest.raises(ValueError, match="regress"):
        running.transition(WorkerStatus.HALTED, NOW, {})
    halted = running.transition(WorkerStatus.HALTED, NOW + timedelta(seconds=2), {})
    with pytest.raises(RuntimeError, match="terminal"):
        halted.transition(WorkerStatus.STARTING, NOW + timedelta(seconds=3), {})
