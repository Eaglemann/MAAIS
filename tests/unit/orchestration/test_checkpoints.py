from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.orchestration.checkpoints import (
    WorkerCheckpoint,
    WorkerLease,
    WorkerLeaseStatus,
    WorkerStatus,
)

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


def test_worker_lease_is_valid_only_before_expiry() -> None:
    lease = WorkerLease(
        experiment_id=UUID(int=1),
        worker_id=UUID(int=2),
        status=WorkerLeaseStatus.ACTIVE,
        acquired_at=NOW,
        heartbeat_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        released_at=None,
        epoch=1,
    )

    assert lease.valid_at(NOW + timedelta(seconds=29))
    assert not lease.valid_at(NOW + timedelta(seconds=30))


def test_released_worker_lease_requires_ordered_release_metadata() -> None:
    with pytest.raises(ValueError, match="ordered release"):
        WorkerLease(
            experiment_id=UUID(int=1),
            worker_id=UUID(int=2),
            status=WorkerLeaseStatus.RELEASED,
            acquired_at=NOW,
            heartbeat_at=NOW + timedelta(seconds=2),
            expires_at=NOW + timedelta(seconds=1),
            released_at=NOW + timedelta(seconds=1),
            epoch=1,
        )
