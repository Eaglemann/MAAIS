from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import WorkerCheckpointModel
from maais.db.repositories.events import EventRepository
from maais.db.repositories.market_data import (
    OperationalPersistResult,
    OperationalStateConflict,
    StaleOperationalState,
    _event_object,
    _json_object,
    _new_event,
    _parse_datetime,
)
from maais.domain.json import MutableJsonValue, content_hash
from maais.orchestration.checkpoints import (
    CheckpointTransition,
    WorkerCheckpoint,
    WorkerStatus,
)


def _checkpoint_state(checkpoint: WorkerCheckpoint) -> dict[str, MutableJsonValue]:
    return _json_object(
        {
            "experiment_id": checkpoint.experiment_id,
            "worker_id": checkpoint.worker_id,
            "status": checkpoint.status,
            "state": checkpoint.state,
            "checkpoint_at": checkpoint.checkpoint_at,
            "version": checkpoint.version,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "event_at": event.event_at,
                    "payload": event.payload,
                }
                for event in checkpoint.events
            ],
        }
    )


def _checkpoint_from_state(state: Mapping[str, object]) -> WorkerCheckpoint:
    raw_events = cast(list[Mapping[str, object]], state["events"])
    return WorkerCheckpoint(
        experiment_id=UUID(str(state["experiment_id"])),
        worker_id=UUID(str(state["worker_id"])),
        status=WorkerStatus(str(state["status"])),
        state=_event_object(state["state"]),
        checkpoint_at=_parse_datetime(state["checkpoint_at"]),
        version=int(cast(str | int, state["version"])),
        events=tuple(
            CheckpointTransition(
                sequence=int(cast(str | int, event["sequence"])),
                event_type=str(event["event_type"]),
                event_at=_parse_datetime(event["event_at"]),
                payload=_event_object(event["payload"]),
            )
            for event in raw_events
        ),
    )


class OrchestrationRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def record_checkpoint(self, checkpoint: WorkerCheckpoint) -> OperationalPersistResult:
        state = _checkpoint_state(checkpoint)
        state_hash = content_hash(state)
        inserted_id = await self._session.scalar(
            insert(WorkerCheckpointModel)
            .values(**self._values(checkpoint, state, state_hash))
            .on_conflict_do_nothing(index_elements=[WorkerCheckpointModel.experiment_id])
            .returning(WorkerCheckpointModel.experiment_id)
        )
        created = inserted_id is not None
        previous_version = 0
        if created:
            new_transitions = checkpoint.events
        else:
            existing = await self._session.scalar(
                select(WorkerCheckpointModel)
                .where(WorkerCheckpointModel.experiment_id == checkpoint.experiment_id)
                .with_for_update()
            )
            if existing is None:
                raise RuntimeError("worker checkpoint disappeared after conflict")
            if existing.worker_id != checkpoint.worker_id:
                raise OperationalStateConflict("worker checkpoint belongs to another worker")
            previous_version = existing.version
            if checkpoint.version < previous_version:
                raise StaleOperationalState("worker checkpoint is older than persisted state")
            if checkpoint.version == previous_version:
                if existing.content_hash != state_hash:
                    raise OperationalStateConflict("checkpoint version has different content")
                return OperationalPersistResult(
                    False, checkpoint.experiment_id, checkpoint.version, state_hash
                )
            new_transitions = tuple(
                event for event in checkpoint.events if event.sequence > previous_version
            )
            if (
                checkpoint.version != previous_version + len(new_transitions)
                or not new_transitions
                or new_transitions[0].sequence != previous_version + 1
            ):
                raise StaleOperationalState("checkpoint transitions are not contiguous")
            for key, value in self._values(checkpoint, state, state_hash).items():
                if key != "experiment_id":
                    setattr(existing, key, value)

        await self._events.append(
            checkpoint.experiment_id,
            "worker_checkpoint",
            previous_version,
            tuple(
                _new_event(
                    aggregate_id=checkpoint.experiment_id,
                    aggregate_type="worker_checkpoint",
                    event_type=event.event_type,
                    payload=event.payload,
                    occurred_at=event.event_at,
                )
                for event in new_transitions
            ),
        )
        return OperationalPersistResult(
            created, checkpoint.experiment_id, checkpoint.version, state_hash
        )

    async def get_checkpoint(self, experiment_id: UUID) -> WorkerCheckpoint:
        row = await self._session.get(WorkerCheckpointModel, experiment_id)
        if row is None:
            raise LookupError("worker checkpoint does not exist")
        return _checkpoint_from_state(cast(Mapping[str, object], row.state_json))

    @staticmethod
    def _values(
        checkpoint: WorkerCheckpoint,
        state: dict[str, MutableJsonValue],
        state_hash: str,
    ) -> dict[str, object]:
        return {
            "experiment_id": checkpoint.experiment_id,
            "worker_id": checkpoint.worker_id,
            "status": checkpoint.status.value,
            "state_json": state,
            "content_hash": state_hash,
            "checkpoint_at": checkpoint.checkpoint_at,
            "version": checkpoint.version,
        }
