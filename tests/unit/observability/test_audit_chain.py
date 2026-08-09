from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.observability.audit import (
    AuditEvent,
    AuditSourceRole,
    HealthEvaluation,
    HealthSeverity,
    HealthStatus,
    bounded_reason_code,
    health_deduplication_key,
    pseudonymous_reference,
    verify_audit_chain,
)

NOW = datetime(2026, 8, 9, 1, tzinfo=timezone.utc)
EVENT_ONE = UUID("11111111-1111-4111-8111-111111111111")
EVENT_TWO = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
BOOT_ID = UUID("44444444-4444-4444-8444-444444444444")
EVALUATION_ONE = UUID("55555555-5555-4555-8555-555555555555")
EVALUATION_TWO = UUID("66666666-6666-4666-8666-666666666666")


def _audit_event(
    *,
    event_id: UUID,
    sequence: int,
    previous_hash: str | None,
    evidence: dict[str, object] | None = None,
) -> AuditEvent:
    return AuditEvent.create(
        event_id=event_id,
        sequence=sequence,
        previous_hash=previous_hash,
        source_role=AuditSourceRole.WEB,
        actor_reference=pseudonymous_reference("actor", "sole_operator"),
        session_reference=pseudonymous_reference("session", EVENT_ONE),
        event_code="auth.login.succeeded",
        reason_code="valid_credentials",
        evidence=evidence or {"authentication": "password"},
        run_id=None,
        service_boot_id=None,
        occurred_at=NOW + timedelta(microseconds=sequence),
    )


def test_audit_hash_binds_previous_hash_and_payload() -> None:
    first = _audit_event(event_id=EVENT_ONE, sequence=1, previous_hash=None)
    second = _audit_event(
        event_id=EVENT_TWO,
        sequence=2,
        previous_hash=first.content_hash,
    )
    altered = _audit_event(
        event_id=EVENT_TWO,
        sequence=2,
        previous_hash=first.content_hash,
        evidence={"authentication": "passkey"},
    )

    assert first.previous_hash is None
    assert second.previous_hash == first.content_hash
    assert second.content_hash != first.content_hash
    assert altered.content_hash != second.content_hash


def test_audit_hash_is_deterministic_across_mapping_order() -> None:
    left = _audit_event(
        event_id=EVENT_ONE,
        sequence=1,
        previous_hash=None,
        evidence={"outer": {"b": 2, "a": 1}, "list": [3, 2, 1]},
    )
    right = _audit_event(
        event_id=EVENT_ONE,
        sequence=1,
        previous_hash=None,
        evidence={"list": [3, 2, 1], "outer": {"a": 1, "b": 2}},
    )

    assert left == right
    assert left.content_hash == right.content_hash


def test_chain_verification_reports_sequence_gap_and_previous_hash_mismatch() -> None:
    first = _audit_event(event_id=EVENT_ONE, sequence=1, previous_hash=None)
    gap = _audit_event(
        event_id=EVENT_TWO,
        sequence=3,
        previous_hash=first.content_hash,
    )
    mismatch = _audit_event(
        event_id=EVENT_TWO,
        sequence=2,
        previous_hash="f" * 64,
    )

    gap_report = verify_audit_chain((first, gap))
    mismatch_report = verify_audit_chain((first, mismatch))

    assert gap_report.ok is False
    assert gap_report.errors == ("audit_sequence_gap:expected=2:actual=3",)
    assert mismatch_report.ok is False
    assert mismatch_report.errors == ("audit_previous_hash_mismatch:sequence=2",)


def test_pseudonymous_references_are_stable_bounded_and_namespace_separated() -> None:
    actor = pseudonymous_reference("actor", "sole_operator")
    repeated = pseudonymous_reference("actor", "sole_operator")
    session = pseudonymous_reference("session", "sole_operator")

    assert actor == repeated
    assert actor != session
    assert actor.startswith("actor:")
    assert len(actor) == len("actor:") + 32
    assert "sole_operator" not in actor
    with pytest.raises(ValueError, match="namespace"):
        pseudonymous_reference("INVALID namespace", "value")


def test_reason_codes_accept_only_stable_bounded_codes() -> None:
    assert bounded_reason_code("worker_restarted", fallback="run_invalidated") == (
        "worker_restarted"
    )
    assert bounded_reason_code("operator wrote free text", fallback="run_invalidated") == (
        "run_invalidated"
    )
    assert bounded_reason_code("x" * 129, fallback="run_invalidated") == "run_invalidated"


def test_health_evaluation_hashes_complete_snapshot_and_recovery_link() -> None:
    failed_checks = ("worker_lease", "ledger")
    unhealthy = HealthEvaluation.create(
        evaluation_id=EVALUATION_ONE,
        run_id=RUN_ID,
        service_boot_id=BOOT_ID,
        overall_status=HealthStatus.CRITICAL,
        failed_check_names=failed_checks,
        severity=HealthSeverity.CRITICAL,
        deduplication_key=health_deduplication_key(RUN_ID, failed_checks),
        incident_id=EVENT_ONE,
        recovery_of_evaluation_id=None,
        recovered_at=None,
        components={
            "ledger": {"status": "failed", "reason_code": "ledger_hash_mismatch"},
            "worker_lease": {"status": "failed", "reason_code": "lease_expired"},
        },
        checked_at=NOW,
    )
    recovered = HealthEvaluation.create(
        evaluation_id=EVALUATION_TWO,
        run_id=RUN_ID,
        service_boot_id=BOOT_ID,
        overall_status=HealthStatus.HEALTHY,
        failed_check_names=(),
        severity=HealthSeverity.INFO,
        deduplication_key=health_deduplication_key(RUN_ID, ()),
        incident_id=None,
        recovery_of_evaluation_id=unhealthy.evaluation_id,
        recovered_at=NOW + timedelta(minutes=1),
        components={"ledger": {"status": "ok"}, "worker_lease": {"status": "ok"}},
        checked_at=NOW + timedelta(minutes=1),
    )

    assert unhealthy.failed_check_names == ("ledger", "worker_lease")
    assert recovered.recovery_of_evaluation_id == unhealthy.evaluation_id
    assert recovered.content_hash != unhealthy.content_hash


@pytest.mark.parametrize(
    ("status", "failed", "severity", "match"),
    (
        (HealthStatus.HEALTHY, ("ledger",), HealthSeverity.INFO, "healthy"),
        (HealthStatus.CRITICAL, (), HealthSeverity.CRITICAL, "failed checks"),
        (HealthStatus.WARNING, ("backup",), HealthSeverity.INFO, "severity"),
    ),
)
def test_health_evaluation_rejects_incoherent_status(
    status: HealthStatus,
    failed: tuple[str, ...],
    severity: HealthSeverity,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        HealthEvaluation.create(
            evaluation_id=EVALUATION_ONE,
            run_id=RUN_ID,
            service_boot_id=BOOT_ID,
            overall_status=status,
            failed_check_names=failed,
            severity=severity,
            deduplication_key=health_deduplication_key(RUN_ID, failed),
            incident_id=None,
            recovery_of_evaluation_id=None,
            recovered_at=None,
            components={"ledger": {"status": "ok"}},
            checked_at=NOW,
        )
