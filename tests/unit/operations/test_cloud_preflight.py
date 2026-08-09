from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from maais.operations.cloud_evidence import (
    CloudEvidenceError,
    CloudEvidenceSnapshot,
    CloudGateEvidence,
)
from maais.operations.cloud_preflight import (
    CLOUD_PREFLIGHT_GATES,
    EXISTING_PREFLIGHT_GATES,
    evaluate_cloud_preflight,
    load_verified_cloud_preflight,
    write_cloud_preflight_bundle,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 9, 20, 0, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-4000-8000-000000000101")
EXPERIMENT_ID = UUID("00000000-0000-4000-8000-000000000102")
OPERATION_ID = UUID("00000000-0000-4000-8000-000000000103")
CANDIDATE_HASH = "1" * 64
MANIFEST_HASH = "2" * 64
DATABASE_HASH = "3" * 64


def _local_preflight(*, passed: bool = True) -> dict[str, object]:
    checks = [
        {"name": name, "passed": passed, "detail": f"local_{name}"}
        for name in EXISTING_PREFLIGHT_GATES
    ]
    return {
        "passed": passed,
        "experiment_id": str(EXPERIMENT_ID),
        "manifest_hash": MANIFEST_HASH,
        "run_purpose": "soak",
        "safety": {"paper_trading_only": True, "live_money": False},
        "checks": checks,
    }


def _snapshot(
    *,
    gates: tuple[CloudGateEvidence, ...] | None = None,
    captured_at: datetime = NOW - timedelta(minutes=1),
    candidate_hash: str = CANDIDATE_HASH,
    run_id: UUID = RUN_ID,
    environment: str = "production",
) -> CloudEvidenceSnapshot:
    return CloudEvidenceSnapshot.create(
        operation_id=OPERATION_ID,
        environment=environment,
        candidate_hash=candidate_hash,
        run_id=run_id,
        experiment_id=EXPERIMENT_ID,
        manifest_hash=MANIFEST_HASH,
        database_system_identifier_sha256=DATABASE_HASH,
        captured_at=captured_at,
        service_boot_ids={
            "web": UUID("00000000-0000-4000-8000-000000000111"),
            "worker": UUID("00000000-0000-4000-8000-000000000112"),
            "operations": UUID("00000000-0000-4000-8000-000000000113"),
            "verifier": UUID("00000000-0000-4000-8000-000000000114"),
        },
        source_evidence_hashes={"railway": "4" * 64, "sentry": "5" * 64},
        gates=gates
        or tuple(
            CloudGateEvidence(name=name, passed=True, detail_code="verified")
            for name in CLOUD_PREFLIGHT_GATES
        ),
    )


def _evaluate(snapshot: CloudEvidenceSnapshot) -> dict[str, object]:
    return evaluate_cloud_preflight(
        local_preflight=_local_preflight(),
        snapshot=snapshot,
        expected_candidate_hash=CANDIDATE_HASH,
        expected_run_id=RUN_ID,
        expected_experiment_id=EXPERIMENT_ID,
        expected_manifest_hash=MANIFEST_HASH,
        expected_environment="production",
        evaluated_at=NOW,
    )


def test_cloud_gate_contract_is_exact() -> None:
    assert EXISTING_PREFLIGHT_GATES == (
        "manifest_mode",
        "runtime_policy",
        "manifest_candidate_identity",
        "repository_clean",
        "repository_identity",
        "run_mode",
        "exchange_credentials_absent",
        "database_schema",
        "stored_manifest",
        "ledger_consistency",
        "restore_drill",
        "dashboard_build",
        "free_disk",
        "fresh_qualification",
        "process_drill_gate",
        "soak_readiness_gate",
    )
    assert CLOUD_PREFLIGHT_GATES == (
        "railway_identity",
        "european_single_replica_topology",
        "private_service_topology",
        "database_role_probes",
        "operator_auth_boundary",
        "telemetry_redaction_canaries",
        "sentry_delivery",
        "external_monitors",
        "dual_store_retention",
        "audit_chain",
        "cloud_run_registry",
        "restart_sleep_autodeploy_policy",
        "resource_cost_headroom",
    )


def test_complete_cloud_preflight_passes_and_preserves_local_gates() -> None:
    report = _evaluate(_snapshot())

    assert report["passed"] is True
    checks = cast(list[dict[str, object]], report["checks"])
    assert [check["name"] for check in checks] == [
        *EXISTING_PREFLIGHT_GATES,
        *CLOUD_PREFLIGHT_GATES,
    ]
    assert report["snapshot_hash"] == _snapshot().snapshot_hash
    assert report["safety"] == {"paper_trading_only": True, "live_money": False}


@pytest.mark.parametrize("failed_gate", CLOUD_PREFLIGHT_GATES)
def test_each_cloud_gate_fails_closed(failed_gate: str) -> None:
    gates = tuple(
        CloudGateEvidence(
            name=name,
            passed=name != failed_gate,
            detail_code="verified" if name != failed_gate else "provider_unavailable",
        )
        for name in CLOUD_PREFLIGHT_GATES
    )

    report = _evaluate(_snapshot(gates=gates))

    assert report["passed"] is False
    checks = cast(list[dict[str, object]], report["checks"])
    failed = [check for check in checks if check["passed"] is False]
    assert [check["name"] for check in failed] == [failed_gate]
    assert failed[0]["detail"] == "provider_unavailable"


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot(captured_at=NOW - timedelta(minutes=16)), "snapshot_stale"),
        (_snapshot(candidate_hash="a" * 64), "candidate_mismatch"),
        (
            _snapshot(run_id=UUID("00000000-0000-4000-8000-000000000199")),
            "run_mismatch",
        ),
        (_snapshot(environment="qualification"), "environment_mismatch"),
    ],
)
def test_snapshot_identity_or_freshness_failure_fails_every_cloud_gate(
    snapshot: CloudEvidenceSnapshot,
    reason: str,
) -> None:
    report = _evaluate(snapshot)
    checks = cast(list[dict[str, object]], report["checks"])
    cloud_checks = checks[len(EXISTING_PREFLIGHT_GATES) :]

    assert report["passed"] is False
    assert all(check["passed"] is False for check in cloud_checks)
    assert {check["detail"] for check in cloud_checks} == {reason}


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_snapshot_rejects_non_exact_gate_inventory(mutation: str) -> None:
    gates = [
        CloudGateEvidence(name=name, passed=True, detail_code="verified")
        for name in CLOUD_PREFLIGHT_GATES
    ]
    if mutation == "missing":
        gates.pop()
    elif mutation == "duplicate":
        gates[-1] = gates[0]
    else:
        gates[-1] = CloudGateEvidence(
            name="unexpected_provider_gate",
            passed=True,
            detail_code="verified",
        )

    with pytest.raises(CloudEvidenceError, match="gate inventory"):
        evaluate_cloud_preflight(
            local_preflight=_local_preflight(),
            snapshot=_snapshot(gates=tuple(gates)),
            expected_candidate_hash=CANDIDATE_HASH,
            expected_run_id=RUN_ID,
            expected_experiment_id=EXPERIMENT_ID,
            expected_manifest_hash=MANIFEST_HASH,
            expected_environment="production",
            evaluated_at=NOW,
        )


def test_snapshot_loader_rejects_hash_tampering() -> None:
    payload = _snapshot().to_dict()
    payload["environment"] = "qualification"

    with pytest.raises(CloudEvidenceError, match="snapshot hash"):
        CloudEvidenceSnapshot.from_dict(payload)


def test_cloud_preflight_bundle_round_trip_is_hash_verified(tmp_path: Path) -> None:
    report = _evaluate(_snapshot())
    paths = write_cloud_preflight_bundle(report, tmp_path)

    loaded, verified = load_verified_cloud_preflight(paths.directory)

    assert verified is True
    assert loaded == report


def test_local_gate_inventory_cannot_be_renamed_or_reordered() -> None:
    local = _local_preflight()
    checks = cast(list[dict[str, object]], local["checks"])
    local["checks"] = list(reversed(checks))

    with pytest.raises(CloudEvidenceError, match="local preflight gate inventory"):
        evaluate_cloud_preflight(
            local_preflight=local,
            snapshot=_snapshot(),
            expected_candidate_hash=CANDIDATE_HASH,
            expected_run_id=RUN_ID,
            expected_experiment_id=EXPERIMENT_ID,
            expected_manifest_hash=MANIFEST_HASH,
            expected_environment="production",
            evaluated_at=NOW,
        )
