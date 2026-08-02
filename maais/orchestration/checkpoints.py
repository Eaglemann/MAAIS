from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from maais.domain.json import JsonValue, freeze_json


class WorkerStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    RECOVERING = "recovering"
    STOPPING = "stopping"
    STOPPED = "stopped"
    HALTED = "halted"


class WorkerLeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class WorkerLease:
    experiment_id: UUID
    worker_id: UUID
    status: WorkerLeaseStatus
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None
    epoch: int

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or self.worker_id.int == 0:
            raise ValueError("worker lease UUIDs cannot be nil")
        for value in (self.acquired_at, self.heartbeat_at, self.expires_at):
            _require_utc(value)
        if self.released_at is not None:
            _require_utc(self.released_at)
        if self.epoch <= 0 or self.heartbeat_at < self.acquired_at:
            raise ValueError("worker lease epoch or heartbeat is invalid")
        if self.status is WorkerLeaseStatus.ACTIVE:
            if self.released_at is not None or self.expires_at <= self.heartbeat_at:
                raise ValueError("active worker lease requires a future expiry")
        elif self.released_at is None or self.released_at < self.heartbeat_at:
            raise ValueError("released worker lease requires an ordered release time")

    @property
    def active(self) -> bool:
        return self.status is WorkerLeaseStatus.ACTIVE

    def valid_at(self, observed_at: datetime) -> bool:
        _require_utc(observed_at)
        return self.active and observed_at < self.expires_at


_ALLOWED_TRANSITIONS: Mapping[WorkerStatus, frozenset[WorkerStatus]] = {
    WorkerStatus.STARTING: frozenset({WorkerStatus.RUNNING, WorkerStatus.HALTED}),
    WorkerStatus.RUNNING: frozenset(
        {WorkerStatus.RECOVERING, WorkerStatus.STOPPING, WorkerStatus.HALTED}
    ),
    WorkerStatus.RECOVERING: frozenset(
        {WorkerStatus.RUNNING, WorkerStatus.STOPPING, WorkerStatus.HALTED}
    ),
    WorkerStatus.STOPPING: frozenset({WorkerStatus.STOPPED, WorkerStatus.HALTED}),
    WorkerStatus.STOPPED: frozenset(),
    WorkerStatus.HALTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CheckpointTransition:
    sequence: int
    event_type: str
    event_at: datetime
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or "." not in self.event_type:
            raise ValueError("checkpoint transition identity is invalid")
        _require_utc(self.event_at)
        object.__setattr__(self, "payload", _payload(self.payload))


@dataclass(frozen=True, slots=True)
class WorkerCheckpoint:
    experiment_id: UUID
    worker_id: UUID
    status: WorkerStatus
    state: Mapping[str, JsonValue]
    checkpoint_at: datetime
    version: int
    events: tuple[CheckpointTransition, ...]

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or self.worker_id.int == 0:
            raise ValueError("checkpoint UUIDs cannot be nil")
        _require_utc(self.checkpoint_at)
        if self.version <= 0:
            raise ValueError("checkpoint version must be positive")
        normalized = _payload(self.state)
        object.__setattr__(self, "state", normalized)
        if len(self.events) != self.version or tuple(
            event.sequence for event in self.events
        ) != tuple(range(1, self.version + 1)):
            raise ValueError("checkpoint event history must be contiguous")
        if self.events[0].event_type != "worker_checkpoint.starting":
            raise ValueError("checkpoint history must begin with starting")
        if self.events[-1].event_at != self.checkpoint_at:
            raise ValueError("checkpoint time must match the latest event")
        if any(
            current.event_at < previous.event_at
            for previous, current in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("checkpoint event time cannot regress")

    @classmethod
    def create(
        cls,
        *,
        experiment_id: UUID,
        worker_id: UUID,
        checkpoint_at: datetime,
        state: Mapping[str, object],
    ) -> WorkerCheckpoint:
        normalized = _payload(state)
        event = CheckpointTransition(
            sequence=1,
            event_type="worker_checkpoint.starting",
            event_at=checkpoint_at,
            payload=normalized,
        )
        return cls(
            experiment_id=experiment_id,
            worker_id=worker_id,
            status=WorkerStatus.STARTING,
            state=normalized,
            checkpoint_at=checkpoint_at,
            version=1,
            events=(event,),
        )

    def transition(
        self,
        status: WorkerStatus,
        checkpoint_at: datetime,
        state: Mapping[str, object],
    ) -> WorkerCheckpoint:
        if not _ALLOWED_TRANSITIONS[self.status]:
            raise RuntimeError("worker checkpoint is terminal")
        if status not in _ALLOWED_TRANSITIONS[self.status]:
            raise RuntimeError(f"illegal worker transition: {self.status.value} -> {status.value}")
        _require_utc(checkpoint_at)
        if checkpoint_at < self.checkpoint_at:
            raise ValueError("checkpoint time cannot regress")
        normalized = _payload(state)
        event = CheckpointTransition(
            sequence=self.version + 1,
            event_type=f"worker_checkpoint.{status.value}",
            event_at=checkpoint_at,
            payload=normalized,
        )
        return replace(
            self,
            status=status,
            state=normalized,
            checkpoint_at=checkpoint_at,
            version=self.version + 1,
            events=(*self.events, event),
        )

    def snapshot(
        self,
        checkpoint_at: datetime,
        state: Mapping[str, object],
    ) -> WorkerCheckpoint:
        if self.status in {WorkerStatus.STOPPED, WorkerStatus.HALTED}:
            raise RuntimeError("worker checkpoint is terminal")
        if self.status not in {WorkerStatus.RUNNING, WorkerStatus.RECOVERING}:
            raise RuntimeError("only an active worker checkpoint can be snapshotted")
        _require_utc(checkpoint_at)
        if checkpoint_at <= self.checkpoint_at:
            raise ValueError("checkpoint snapshot time must advance")
        normalized = _payload(state)
        event = CheckpointTransition(
            sequence=self.version + 1,
            event_type="worker_checkpoint.snapshotted",
            event_at=checkpoint_at,
            payload=normalized,
        )
        return replace(
            self,
            state=normalized,
            checkpoint_at=checkpoint_at,
            version=self.version + 1,
            events=(*self.events, event),
        )

    def restart(
        self,
        *,
        worker_id: UUID,
        checkpoint_at: datetime,
        state: Mapping[str, object],
    ) -> WorkerCheckpoint:
        if worker_id.int == 0 or worker_id == self.worker_id:
            raise ValueError("checkpoint restart requires a different non-nil worker")
        _require_utc(checkpoint_at)
        if checkpoint_at < self.checkpoint_at:
            raise ValueError("checkpoint time cannot regress")
        normalized = _payload(state)
        event = CheckpointTransition(
            sequence=self.version + 1,
            event_type="worker_checkpoint.starting",
            event_at=checkpoint_at,
            payload={
                **normalized,
                "previous_worker_id": str(self.worker_id),
                "previous_status": self.status.value,
            },
        )
        return replace(
            self,
            worker_id=worker_id,
            status=WorkerStatus.STARTING,
            state=normalized,
            checkpoint_at=checkpoint_at,
            version=self.version + 1,
            events=(*self.events, event),
        )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("checkpoint time must be UTC-aware")


def _payload(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("checkpoint state must be an object")
    return normalized
