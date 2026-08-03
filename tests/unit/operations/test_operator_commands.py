from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.operations.operator_commands import (
    CommandStatus,
    CommandType,
    OperatorCommand,
)

NOW = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)
COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


def _request(
    command_type: CommandType = CommandType.EMERGENCY_HALT,
    *,
    confirmation: str | None = "CONFIRM EMERGENCY_HALT",
) -> OperatorCommand:
    return OperatorCommand.request(
        command_id=COMMAND_ID,
        experiment_id=EXPERIMENT_ID,
        command_type=command_type,
        idempotency_key="33333333-3333-4333-8333-333333333333",
        actor="local_operator",
        reason="operator observed abnormal behavior",
        payload={"source": "mission_control"},
        confirmation=confirmation,
        requested_at=NOW,
    )


def test_safety_critical_command_requires_its_exact_confirmation_phrase() -> None:
    with pytest.raises(ValueError, match="CONFIRM EMERGENCY_HALT"):
        _request(confirmation=None)
    with pytest.raises(ValueError, match="CONFIRM EMERGENCY_HALT"):
        _request(confirmation="yes")

    command = _request()

    assert command.status is CommandStatus.REQUESTED
    assert command.operator_confirmed is True
    expected_request_hash = "".join(
        (
            "d7f1f4fc7089d86d",  # pragma: allowlist secret
            "a175afd29267273c",  # pragma: allowlist secret
            "a6cc273c88e7be43",  # pragma: allowlist secret
            "a57b074cb21b285e",  # pragma: allowlist secret
        )
    )
    assert command.request_hash == expected_request_hash


def test_command_lifecycle_records_worker_acceptance_and_terminal_result() -> None:
    requested = _request(CommandType.PAUSE, confirmation="CONFIRM PAUSE")

    accepted = requested.accept(
        accepted_at=NOW + timedelta(seconds=1),
        worker_id="paper_worker:44444444-4444-4444-8444-444444444444",
    )
    completed = accepted.complete(
        completed_at=NOW + timedelta(seconds=2),
        result={"experiment_status": "paused", "kill_switch_active": True},
    )

    assert accepted.status is CommandStatus.ACCEPTED
    assert accepted.version == 2
    assert accepted.accepted_by == "paper_worker:44444444-4444-4444-8444-444444444444"
    assert completed.status is CommandStatus.COMPLETED
    assert completed.version == 3
    assert completed.result == {
        "experiment_status": "paused",
        "kill_switch_active": True,
    }
    assert (
        completed.complete(
            completed_at=NOW + timedelta(seconds=3),
            result={"experiment_status": "paused", "kill_switch_active": True},
        )
        == completed
    )
    with pytest.raises(ValueError, match="different result"):
        completed.complete(
            completed_at=NOW + timedelta(seconds=3),
            result={"experiment_status": "running"},
        )


def test_accepted_command_can_be_taken_over_and_keeps_original_acceptance_time() -> None:
    requested = _request(CommandType.FLATTEN, confirmation="CONFIRM FLATTEN")
    original_worker = "paper_worker:44444444-4444-4444-8444-444444444444"
    recovery_worker = "paper_worker:55555555-5555-4555-8555-555555555555"
    accepted_at = NOW + timedelta(seconds=1)
    accepted = requested.accept(accepted_at=accepted_at, worker_id=original_worker)

    recovered = accepted.take_over(worker_id=recovery_worker)
    completed = recovered.complete(
        completed_at=NOW + timedelta(seconds=3),
        result={"flattened_positions": 1},
    )

    assert recovered.status is CommandStatus.ACCEPTED
    assert recovered.accepted_at == accepted_at
    assert recovered.accepted_by == recovery_worker
    assert recovered.version == 3
    assert completed.status is CommandStatus.COMPLETED
    assert completed.version == 4


def test_non_safety_incident_acknowledgement_does_not_require_confirmation() -> None:
    command = OperatorCommand.request(
        command_id=COMMAND_ID,
        experiment_id=EXPERIMENT_ID,
        command_type=CommandType.ACKNOWLEDGE_INCIDENT,
        idempotency_key="33333333-3333-4333-8333-333333333333",
        actor="local_operator",
        reason="reviewed incident evidence",
        payload={"incident_id": "55555555-5555-4555-8555-555555555555"},
        confirmation=None,
        requested_at=NOW,
    )

    assert command.operator_confirmed is False


def test_rejected_command_preserves_a_structured_worker_reason() -> None:
    accepted = _request().accept(
        accepted_at=NOW + timedelta(seconds=1),
        worker_id="paper_worker:44444444-4444-4444-8444-444444444444",
    )

    rejected = accepted.reject(
        completed_at=NOW + timedelta(seconds=2),
        reason_code="open_positions_require_flatten",
        detail="reset is unsafe while a simulated position remains open",
    )

    assert rejected.status is CommandStatus.REJECTED
    assert rejected.result == {
        "reason_code": "open_positions_require_flatten",
        "detail": "reset is unsafe while a simulated position remains open",
    }


def test_lifecycle_rejects_partial_acceptance_or_completion_fields() -> None:
    requested = _request()

    with pytest.raises(ValueError, match="acceptance shape"):
        replace(requested, accepted_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="completion"):
        replace(requested, completed_at=NOW + timedelta(seconds=2))
