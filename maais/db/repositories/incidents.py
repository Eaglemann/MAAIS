from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import IncidentModel
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
from maais.operations.incidents import (
    IncidentSeverity,
    IncidentState,
    IncidentStatus,
    IncidentTransition,
)


def _incident_state(incident: IncidentState) -> dict[str, MutableJsonValue]:
    return _json_object(
        {
            "incident_id": incident.incident_id,
            "experiment_id": incident.experiment_id,
            "deduplication_key": incident.deduplication_key,
            "severity": incident.severity,
            "component": incident.component,
            "reason_code": incident.reason_code,
            "evidence": incident.evidence,
            "requires_operator_review": incident.requires_operator_review,
            "status": incident.status,
            "detected_at": incident.detected_at,
            "acknowledged_at": incident.acknowledged_at,
            "resolved_at": incident.resolved_at,
            "acknowledged_by": incident.acknowledged_by,
            "resolved_by": incident.resolved_by,
            "resolution": incident.resolution,
            "changed_at": incident.changed_at,
            "version": incident.version,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "event_at": event.event_at,
                    "payload": event.payload,
                }
                for event in incident.events
            ],
        }
    )


def _incident_from_state(state: Mapping[str, object]) -> IncidentState:
    raw_events = cast(list[Mapping[str, object]], state["events"])
    return IncidentState(
        incident_id=UUID(str(state["incident_id"])),
        experiment_id=UUID(str(state["experiment_id"])),
        deduplication_key=str(state["deduplication_key"]),
        severity=IncidentSeverity(str(state["severity"])),
        component=str(state["component"]),
        reason_code=str(state["reason_code"]),
        evidence=_event_object(state["evidence"]),
        requires_operator_review=bool(state["requires_operator_review"]),
        status=IncidentStatus(str(state["status"])),
        detected_at=_parse_datetime(state["detected_at"]),
        acknowledged_at=(
            _parse_datetime(state["acknowledged_at"])
            if state["acknowledged_at"] is not None
            else None
        ),
        resolved_at=(
            _parse_datetime(state["resolved_at"]) if state["resolved_at"] is not None else None
        ),
        acknowledged_by=cast(str | None, state["acknowledged_by"]),
        resolved_by=cast(str | None, state["resolved_by"]),
        resolution=cast(str | None, state["resolution"]),
        changed_at=_parse_datetime(state["changed_at"]),
        version=int(cast(str | int, state["version"])),
        events=tuple(
            IncidentTransition(
                sequence=int(cast(str | int, event["sequence"])),
                event_type=str(event["event_type"]),
                event_at=_parse_datetime(event["event_at"]),
                payload=_event_object(event["payload"]),
            )
            for event in raw_events
        ),
    )


class IncidentRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def record(self, incident: IncidentState) -> OperationalPersistResult:
        state = _incident_state(incident)
        state_hash = content_hash(state)
        inserted_id = await self._session.scalar(
            insert(IncidentModel)
            .values(**self._values(incident, state, state_hash))
            .on_conflict_do_nothing()
            .returning(IncidentModel.id)
        )
        created = inserted_id is not None
        previous_version = 0
        if created:
            new_transitions = incident.events
        else:
            existing = await self._session.scalar(
                select(IncidentModel)
                .where(IncidentModel.id == incident.incident_id)
                .with_for_update()
            )
            if existing is None:
                duplicate = await self._session.scalar(
                    select(IncidentModel).where(
                        IncidentModel.experiment_id == incident.experiment_id,
                        IncidentModel.deduplication_key == incident.deduplication_key,
                    )
                )
                if duplicate is not None:
                    raise OperationalStateConflict(
                        "incident deduplication key belongs to another incident"
                    )
                raise OperationalStateConflict("incident identity conflicts with stored state")
            if (
                existing.experiment_id != incident.experiment_id
                or existing.deduplication_key != incident.deduplication_key
                or existing.severity != incident.severity.value
                or existing.component != incident.component
                or existing.reason_code != incident.reason_code
                or existing.evidence_json != _json_object(incident.evidence)
                or existing.requires_operator_review != incident.requires_operator_review
                or existing.detected_at != incident.detected_at
            ):
                raise OperationalStateConflict("incident immutable identity has changed")
            previous_version = existing.version
            if incident.version < previous_version:
                raise StaleOperationalState("incident state is older than persisted state")
            if incident.version == previous_version:
                if existing.content_hash != state_hash:
                    raise OperationalStateConflict("incident version has different content")
                return OperationalPersistResult(
                    False, incident.incident_id, incident.version, state_hash
                )
            new_transitions = tuple(
                event for event in incident.events if event.sequence > previous_version
            )
            if (
                incident.version != previous_version + len(new_transitions)
                or not new_transitions
                or new_transitions[0].sequence != previous_version + 1
            ):
                raise StaleOperationalState("incident transitions are not contiguous")
            for key, value in self._values(incident, state, state_hash).items():
                if key != "id":
                    setattr(existing, key, value)

        await self._events.append(
            incident.incident_id,
            "incident",
            previous_version,
            tuple(
                _new_event(
                    aggregate_id=incident.incident_id,
                    aggregate_type="incident",
                    event_type=event.event_type,
                    payload=event.payload,
                    occurred_at=event.event_at,
                )
                for event in new_transitions
            ),
        )
        return OperationalPersistResult(created, incident.incident_id, incident.version, state_hash)

    async def get(self, incident_id: UUID) -> IncidentState:
        row = await self._session.get(IncidentModel, incident_id)
        if row is None:
            raise LookupError("incident does not exist")
        return _incident_from_state(cast(Mapping[str, object], row.state_json))

    async def get_unresolved(self, experiment_id: UUID) -> tuple[IncidentState, ...]:
        rows = (
            await self._session.scalars(
                select(IncidentModel)
                .where(
                    IncidentModel.experiment_id == experiment_id,
                    IncidentModel.status != IncidentStatus.RESOLVED.value,
                )
                .order_by(IncidentModel.detected_at, IncidentModel.id)
            )
        ).all()
        return tuple(
            _incident_from_state(cast(Mapping[str, object], row.state_json)) for row in rows
        )

    @staticmethod
    def _values(
        incident: IncidentState,
        state: dict[str, MutableJsonValue],
        state_hash: str,
    ) -> dict[str, object]:
        return {
            "id": incident.incident_id,
            "experiment_id": incident.experiment_id,
            "deduplication_key": incident.deduplication_key,
            "severity": incident.severity.value,
            "component": incident.component,
            "reason_code": incident.reason_code,
            "evidence_json": _json_object(incident.evidence),
            "requires_operator_review": incident.requires_operator_review,
            "status": incident.status.value,
            "detected_at": incident.detected_at,
            "acknowledged_at": incident.acknowledged_at,
            "resolved_at": incident.resolved_at,
            "acknowledged_by": incident.acknowledged_by,
            "resolved_by": incident.resolved_by,
            "resolution": incident.resolution,
            "changed_at": incident.changed_at,
            "version": incident.version,
            "state_json": state,
            "content_hash": state_hash,
        }
