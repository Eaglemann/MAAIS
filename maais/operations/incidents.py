from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from maais.domain.json import JsonValue, freeze_json


class IncidentSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class IncidentStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class IncidentTransition:
    sequence: int
    event_type: str
    event_at: datetime
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or "." not in self.event_type:
            raise ValueError("incident transition identity is invalid")
        _require_utc(self.event_at, "event_at")
        object.__setattr__(self, "payload", _payload(self.payload))


@dataclass(frozen=True, slots=True)
class IncidentState:
    incident_id: UUID
    experiment_id: UUID
    deduplication_key: str
    severity: IncidentSeverity
    component: str
    reason_code: str
    evidence: Mapping[str, JsonValue]
    requires_operator_review: bool
    status: IncidentStatus
    detected_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    acknowledged_by: str | None
    resolved_by: str | None
    resolution: str | None
    changed_at: datetime
    version: int
    events: tuple[IncidentTransition, ...]

    def __post_init__(self) -> None:
        if self.incident_id.int == 0 or self.experiment_id.int == 0:
            raise ValueError("incident UUIDs cannot be nil")
        if not self.deduplication_key or not self.component or not self.reason_code:
            raise ValueError("incident identity and reason are required")
        for value, field in (
            (self.detected_at, "detected_at"),
            (self.changed_at, "changed_at"),
        ):
            _require_utc(value, field)
        for value, field in (
            (self.acknowledged_at, "acknowledged_at"),
            (self.resolved_at, "resolved_at"),
        ):
            if value is not None:
                _require_utc(value, field)
        if self.changed_at < self.detected_at:
            raise ValueError("incident change time cannot precede detection")
        if self.status is IncidentStatus.OPEN and (
            self.acknowledged_at is not None or self.resolved_at is not None
        ):
            raise ValueError("open incident cannot have terminal transition times")
        if self.status is IncidentStatus.ACKNOWLEDGED and (
            self.acknowledged_at is None or self.resolved_at is not None
        ):
            raise ValueError("acknowledged incident transition times are invalid")
        if self.status is IncidentStatus.RESOLVED and self.resolved_at is None:
            raise ValueError("resolved incident requires a resolution time")
        if (self.acknowledged_at is None) != (self.acknowledged_by is None):
            raise ValueError("incident acknowledgement time and actor must appear together")
        if self.resolved_at is None:
            if self.resolved_by is not None or self.resolution is not None:
                raise ValueError("unresolved incident cannot have resolution metadata")
        elif not self.resolved_by or not self.resolution:
            raise ValueError("resolved incident requires actor and resolution")
        if (
            self.version <= 0
            or len(self.events) != self.version
            or tuple(event.sequence for event in self.events) != tuple(range(1, self.version + 1))
        ):
            raise ValueError("incident event history must be contiguous")
        if self.events[0].event_type != "incident.opened":
            raise ValueError("incident history must begin with incident.opened")
        if self.events[-1].event_at != self.changed_at:
            raise ValueError("incident change time must match the latest event")
        if any(
            current.event_at < previous.event_at
            for previous, current in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("incident event time cannot regress")
        normalized = freeze_json(self.evidence)
        if not isinstance(normalized, Mapping):
            raise TypeError("incident evidence must be an object")
        object.__setattr__(self, "evidence", normalized)

    @classmethod
    def create(
        cls,
        *,
        incident_id: UUID,
        experiment_id: UUID,
        deduplication_key: str,
        severity: IncidentSeverity,
        component: str,
        reason_code: str,
        evidence: Mapping[str, object],
        requires_operator_review: bool,
        detected_at: datetime,
    ) -> IncidentState:
        _require_utc(detected_at, "detected_at")
        normalized = _payload(evidence)
        event = IncidentTransition(
            sequence=1,
            event_type="incident.opened",
            event_at=detected_at,
            payload=_payload(
                {
                    "deduplication_key": deduplication_key,
                    "severity": severity,
                    "component": component,
                    "reason_code": reason_code,
                    "evidence": normalized,
                    "requires_operator_review": requires_operator_review,
                }
            ),
        )
        return cls(
            incident_id=incident_id,
            experiment_id=experiment_id,
            deduplication_key=deduplication_key,
            severity=severity,
            component=component,
            reason_code=reason_code,
            evidence=normalized,
            requires_operator_review=requires_operator_review,
            status=IncidentStatus.OPEN,
            detected_at=detected_at,
            acknowledged_at=None,
            resolved_at=None,
            acknowledged_by=None,
            resolved_by=None,
            resolution=None,
            changed_at=detected_at,
            version=1,
            events=(event,),
        )

    def acknowledge(self, actor: str, acknowledged_at: datetime) -> IncidentState:
        if self.status is IncidentStatus.RESOLVED:
            raise RuntimeError("incident is already resolved")
        if self.status is IncidentStatus.ACKNOWLEDGED:
            raise RuntimeError("incident is already acknowledged")
        if not actor:
            raise ValueError("incident acknowledgement actor is required")
        return self._advance(
            status=IncidentStatus.ACKNOWLEDGED,
            event_type="incident.acknowledged",
            event_at=acknowledged_at,
            payload={"actor": actor},
            acknowledged_at=acknowledged_at,
            acknowledged_by=actor,
        )

    def resolve(
        self,
        actor: str,
        resolution: str,
        resolved_at: datetime,
        *,
        operator_confirmed: bool,
    ) -> IncidentState:
        if self.status is IncidentStatus.RESOLVED:
            raise RuntimeError("incident is already resolved")
        if not actor or not resolution:
            raise ValueError("incident resolution actor and text are required")
        if self.requires_operator_review and not operator_confirmed:
            raise PermissionError("incident resolution requires operator confirmation")
        return self._advance(
            status=IncidentStatus.RESOLVED,
            event_type="incident.resolved",
            event_at=resolved_at,
            payload={
                "actor": actor,
                "resolution": resolution,
                "operator_confirmed": operator_confirmed,
            },
            resolved_at=resolved_at,
            resolved_by=actor,
            resolution=resolution,
        )

    def _advance(
        self,
        *,
        status: IncidentStatus,
        event_type: str,
        event_at: datetime,
        payload: object,
        **changes: object,
    ) -> IncidentState:
        _require_utc(event_at, "event_at")
        if event_at < self.changed_at:
            raise ValueError("incident transition time cannot regress")
        event = IncidentTransition(
            sequence=self.version + 1,
            event_type=event_type,
            event_at=event_at,
            payload=_payload(payload),
        )
        return replace(
            self,
            status=status,
            changed_at=event_at,
            version=self.version + 1,
            events=(*self.events, event),
            **changes,
        )


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


def _payload(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("incident payload must be an object")
    return normalized
