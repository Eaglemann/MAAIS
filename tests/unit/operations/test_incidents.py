from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.operations.incidents import IncidentSeverity, IncidentState, IncidentStatus

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _incident(*, review: bool = True) -> IncidentState:
    return IncidentState.create(
        incident_id=UUID(int=1),
        experiment_id=UUID(int=2),
        deduplication_key="exit:position-1:no_eligible_book",
        severity=IncidentSeverity.CRITICAL,
        component="paper_broker",
        reason_code="protective_exit_unfillable",
        evidence={"position_id": str(UUID(int=3)), "reason": "no_eligible_book"},
        requires_operator_review=review,
        detected_at=NOW,
    )


def test_incident_lifecycle_is_versioned_and_evidence_is_immutable() -> None:
    incident = _incident()
    acknowledged = incident.acknowledge("operator", NOW + timedelta(seconds=1))
    resolved = acknowledged.resolve(
        "operator",
        "position flattened and ledger reconciled",
        NOW + timedelta(seconds=2),
        operator_confirmed=True,
    )

    assert incident.status is IncidentStatus.OPEN
    assert acknowledged.status is IncidentStatus.ACKNOWLEDGED
    assert resolved.status is IncidentStatus.RESOLVED
    assert resolved.version == 3
    assert [event.sequence for event in resolved.events] == [1, 2, 3]
    with pytest.raises(TypeError):
        resolved.evidence["reason"] = "changed"  # type: ignore[index]


def test_operator_review_incident_cannot_be_auto_resolved_or_regress() -> None:
    incident = _incident().acknowledge("operator", NOW + timedelta(seconds=1))

    with pytest.raises(PermissionError, match="operator confirmation"):
        incident.resolve(
            "worker",
            "automatic retry succeeded",
            NOW + timedelta(seconds=2),
            operator_confirmed=False,
        )
    with pytest.raises(ValueError, match="regress"):
        incident.resolve(
            "operator",
            "resolved",
            NOW - timedelta(seconds=1),
            operator_confirmed=True,
        )


def test_terminal_incident_rejects_further_transitions() -> None:
    incident = _incident(review=False).resolve(
        "worker",
        "recovered",
        NOW + timedelta(seconds=1),
        operator_confirmed=False,
    )

    with pytest.raises(RuntimeError, match="resolved"):
        incident.acknowledge("operator", NOW + timedelta(seconds=2))
