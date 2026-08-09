from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from maais.operations.cloud_evidence import CloudEvidenceError
from maais.operations.cloud_soak_readiness import (
    CLOUD_SOAK_GATES,
    EXISTING_SOAK_GATES,
    CloudSoakSnapshot,
    evaluate_cloud_soak_readiness,
    load_verified_cloud_soak_readiness,
    write_cloud_soak_readiness_bundle,
)

UTC = timezone.utc
STARTED_AT = datetime(2026, 8, 8, 20, 0, tzinfo=UTC)
EVALUATED_AT = STARTED_AT + timedelta(hours=24, minutes=1)
RUN_ID = UUID("00000000-0000-4000-8000-000000000401")
EXPERIMENT_ID = UUID("00000000-0000-4000-8000-000000000402")
OPERATION_ID = UUID("00000000-0000-4000-8000-000000000403")
CANDIDATE_HASH = "1" * 64
MANIFEST_HASH = "2" * 64
DATABASE_HASH = "3" * 64


def _local_soak() -> dict[str, object]:
    return {
        "passed": True,
        "verdict": "ready_for_seven_day_paper_test",
        "report_id": "4" * 64,
        "experiment": {
            "id": str(EXPERIMENT_ID),
            "manifest_hash": MANIFEST_HASH,
        },
        "safety": {"paper_trading_only": True, "live_money": False},
        "checks": [
            {"name": name, "passed": True, "detail": f"local_{name}"}
            for name in EXISTING_SOAK_GATES
        ],
        "decision_coverage": {
            "decisions": 300,
            "rejections": 240,
            "proposals": 12,
            "orders": 0,
            "fills": 0,
            "counterfactuals": 300,
            "warmup_neutral_decisions": 180,
        },
    }


def _snapshot(**changes: object) -> CloudSoakSnapshot:
    values: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "environment": "production",
        "candidate_hash": CANDIDATE_HASH,
        "run_id": RUN_ID,
        "experiment_id": EXPERIMENT_ID,
        "manifest_hash": MANIFEST_HASH,
        "database_system_identifier_sha256": DATABASE_HASH,
        "activated_at": STARTED_AT,
        "captured_at": EVALUATED_AT,
        "service_boot_ids": {
            "web": (UUID("00000000-0000-4000-8000-000000000411"),),
            "worker": (UUID("00000000-0000-4000-8000-000000000412"),),
            "operations": (UUID("00000000-0000-4000-8000-000000000413"),),
            "verifier": (UUID("00000000-0000-4000-8000-000000000414"),),
        },
        "interruption_events": (),
        "configuration_event_count": 0,
        "health_sample_count": 1_441,
        "expected_health_sample_count": 1_440,
        "newest_health_at": EVALUATED_AT - timedelta(seconds=30),
        "external_monitor_sample_count": 1_441,
        "expected_external_monitor_sample_count": 1_440,
        "sentry_gap_count": 0,
        "audit_chain_valid": True,
        "dual_store_artifacts_valid": True,
        "backup_restore_verified": True,
        "auth_probe_valid": True,
        "resource_cost_headroom": True,
        "source_evidence_hashes": {"railway": "5" * 64, "sentry": "6" * 64},
    }
    values.update(changes)
    return CloudSoakSnapshot.create(**values)  # type: ignore[arg-type]


def _evaluate(snapshot: CloudSoakSnapshot) -> dict[str, object]:
    return evaluate_cloud_soak_readiness(
        local_soak=_local_soak(),
        snapshot=snapshot,
        expected_candidate_hash=CANDIDATE_HASH,
        expected_run_id=RUN_ID,
        expected_experiment_id=EXPERIMENT_ID,
        expected_manifest_hash=MANIFEST_HASH,
        expected_environment="production",
        evaluated_at=EVALUATED_AT,
    )


def test_cloud_soak_gate_contract_is_exact() -> None:
    assert EXISTING_SOAK_GATES == (
        "paper_only_safety",
        "candidate_identity",
        "postgres_cluster_identity",
        "preflight_evidence",
        "pre_soak_process_drills",
        "minimum_duration",
        "process_continuity",
        "runtime_health",
        "ledger_consistency",
        "operational_state",
        "decision_cardinality",
        "decision_metadata_coverage",
        "required_data_quality",
        "structured_logs",
        "daily_report_reconciliation",
    )
    assert CLOUD_SOAK_GATES == (
        "cloud_identity_continuity",
        "external_monitoring",
        "audit_chain_integrity",
        "dual_store_artifacts",
        "backup_restore_evidence",
        "operator_auth_health",
        "resource_cost_headroom",
    )


def test_complete_cloud_soak_passes_with_warmup_and_zero_fills() -> None:
    report = _evaluate(_snapshot())

    assert report["passed"] is True
    assert report["verdict"] == "ready_for_seven_day_paper_test"
    checks = cast(list[dict[str, object]], report["checks"])
    assert [check["name"] for check in checks] == [
        *EXISTING_SOAK_GATES,
        *CLOUD_SOAK_GATES,
    ]
    assert report["decision_coverage"] == _local_soak()["decision_coverage"]


def test_verdict_refuses_to_run_before_exact_24_hours() -> None:
    snapshot = _snapshot(captured_at=STARTED_AT + timedelta(hours=23, minutes=59, seconds=59))

    with pytest.raises(CloudEvidenceError, match="before 24 hours"):
        evaluate_cloud_soak_readiness(
            local_soak=_local_soak(),
            snapshot=snapshot,
            expected_candidate_hash=CANDIDATE_HASH,
            expected_run_id=RUN_ID,
            expected_experiment_id=EXPERIMENT_ID,
            expected_manifest_hash=MANIFEST_HASH,
            expected_environment="production",
            evaluated_at=snapshot.captured_at,
        )


@pytest.mark.parametrize(
    ("changes", "failed_gate", "detail"),
    [
        (
            {
                "service_boot_ids": {
                    "web": (
                        UUID("00000000-0000-4000-8000-000000000411"),
                        UUID("00000000-0000-4000-8000-000000000499"),
                    ),
                    "worker": (UUID("00000000-0000-4000-8000-000000000412"),),
                    "operations": (UUID("00000000-0000-4000-8000-000000000413"),),
                    "verifier": (UUID("00000000-0000-4000-8000-000000000414"),),
                }
            },
            "cloud_identity_continuity",
            "service_boot_changed",
        ),
        (
            {"interruption_events": ("deployment_restarted",)},
            "cloud_identity_continuity",
            "interruption_recorded",
        ),
        (
            {"external_monitor_sample_count": 1_439},
            "external_monitoring",
            "monitor_samples_incomplete",
        ),
        (
            {"sentry_gap_count": 1},
            "external_monitoring",
            "sentry_delivery_gap",
        ),
        ({"audit_chain_valid": False}, "audit_chain_integrity", "audit_chain_invalid"),
        (
            {"dual_store_artifacts_valid": False},
            "dual_store_artifacts",
            "artifact_replication_invalid",
        ),
        (
            {"backup_restore_verified": False},
            "backup_restore_evidence",
            "backup_restore_unverified",
        ),
        ({"auth_probe_valid": False}, "operator_auth_health", "auth_probe_failed"),
        (
            {"resource_cost_headroom": False},
            "resource_cost_headroom",
            "resource_or_cost_cutoff",
        ),
    ],
)
def test_cloud_soak_failure_signals_are_permanent_and_visible(
    changes: dict[str, object],
    failed_gate: str,
    detail: str,
) -> None:
    report = _evaluate(_snapshot(**changes))
    checks = cast(list[dict[str, object]], report["checks"])

    assert report["passed"] is False
    failed = [check for check in checks if check["passed"] is False]
    assert [check["name"] for check in failed] == [failed_gate]
    assert failed[0]["detail"] == detail


def test_snapshot_hash_tampering_is_rejected() -> None:
    payload = _snapshot().to_dict()
    payload["configuration_event_count"] = 1

    with pytest.raises(CloudEvidenceError, match="snapshot hash"):
        CloudSoakSnapshot.from_dict(payload)


def test_cloud_soak_bundle_round_trip_is_verified(tmp_path: Path) -> None:
    report = _evaluate(_snapshot())
    paths = write_cloud_soak_readiness_bundle(report, tmp_path)

    loaded, verified = load_verified_cloud_soak_readiness(paths.directory)

    assert verified is True
    assert loaded == report
