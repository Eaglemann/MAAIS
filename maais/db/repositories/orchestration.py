from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import WorkerCheckpointModel
from maais.db.repositories.counterfactuals import (
    CounterfactualRecordResult,
    CounterfactualRepository,
)
from maais.db.repositories.decisions import DecisionRecordResult, DecisionRepository
from maais.db.repositories.events import EventRepository
from maais.db.repositories.execution import (
    PaperExecutionRepository,
    PaperExecutionResult,
)
from maais.db.repositories.incidents import IncidentRepository
from maais.db.repositories.market_data import (
    MarketDataRepository,
    OperationalPersistResult,
    OperationalStateConflict,
    QualityPersistResult,
    StaleOperationalState,
    _event_object,
    _json_object,
    _new_event,
    _parse_datetime,
)
from maais.domain.json import MutableJsonValue, content_hash
from maais.market_data.integrity.state_machine import IntegrityAssessment, IntegrityCheck
from maais.market_data.recovery import MarketCursor
from maais.orchestration.checkpoints import (
    CheckpointTransition,
    WorkerCheckpoint,
    WorkerStatus,
)
from maais.orchestration.results import OrchestrationOutcome


@dataclass(frozen=True, slots=True)
class OrchestrationPersistResult:
    decision: DecisionRecordResult
    quality: QualityPersistResult
    cursor: OperationalPersistResult | None
    counterfactual: CounterfactualRecordResult | None
    execution: PaperExecutionResult | None
    sensitivity_rows_created: int
    incident: OperationalPersistResult | None


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
        self._decisions = DecisionRepository(session, events)
        self._market_data = MarketDataRepository(session, events)
        self._counterfactuals = CounterfactualRepository(session, events)
        self._paper_execution = PaperExecutionRepository(session, events)
        self._incidents = IncidentRepository(session, events)

    async def record_outcome(
        self,
        outcome: OrchestrationOutcome,
        *,
        integrity: IntegrityAssessment,
        required_checks: frozenset[IntegrityCheck],
        evaluated_at: datetime,
        cursor: MarketCursor | None = None,
    ) -> OrchestrationPersistResult:
        """Record one complete cycle inside the caller-owned database transaction."""

        outcome.bundle.validate()
        frame = outcome.bundle.market_frame
        cycle = outcome.bundle.cycle
        if integrity.frame_id != frame.id:
            raise ValueError("quality assessment and orchestration frame differ")
        if cycle.experiment_id != frame.experiment_id:
            raise ValueError("decision and market frame experiment differ")
        if cursor is not None and (
            cursor.experiment_id != cycle.experiment_id
            or cursor.symbol != cycle.symbol
            or cursor.timeframe != cycle.timeframe
            or cursor.bar_close_at != frame.bar_close_at
        ):
            raise ValueError("cursor does not advance the exact orchestration frame")
        if outcome.execution is not None and outcome.execution.order.proposal_id != (
            outcome.bundle.proposal.id if outcome.bundle.proposal is not None else None
        ):
            raise ValueError("paper execution and proposal identity differ")

        decision_result = await self._decisions.record_bundle(outcome.bundle)
        quality_result = await self._market_data.record_quality(
            integrity,
            evaluated_at=evaluated_at,
            required_checks=required_checks,
        )
        counterfactual_result = (
            await self._counterfactuals.record(outcome.counterfactual)
            if outcome.counterfactual is not None
            else None
        )
        execution_result = (
            await self._paper_execution.record(outcome.execution)
            if outcome.execution is not None
            else None
        )
        sensitivity_rows = 0
        if outcome.execution is not None and outcome.sensitivities:
            sensitivity_rows = await self._paper_execution.record_sensitivities(
                outcome.execution.order.order_id,
                outcome.sensitivities,
            )
        incident_result = (
            await self._incidents.record(outcome.incident) if outcome.incident is not None else None
        )
        cursor_result = (
            await self._market_data.record_cursor(cursor) if cursor is not None else None
        )
        return OrchestrationPersistResult(
            decision=decision_result,
            quality=quality_result,
            cursor=cursor_result,
            counterfactual=counterfactual_result,
            execution=execution_result,
            sensitivity_rows_created=sensitivity_rows,
            incident=incident_result,
        )

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
