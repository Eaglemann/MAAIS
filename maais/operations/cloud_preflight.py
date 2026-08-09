"""Fail-closed cloud readiness evaluation over immutable provider evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from maais.domain.json import content_hash, to_json_data
from maais.operations.cloud_evidence import (
    CloudEvidenceBundlePaths,
    CloudEvidenceError,
    CloudEvidenceSnapshot,
    load_verified_cloud_evidence,
    snapshot_identity_failure,
    validate_exact_gate_inventory,
    write_cloud_evidence_bundle,
)

CLOUD_PREFLIGHT_SCHEMA_VERSION = 1
CLOUD_PREFLIGHT_MAXIMUM_AGE = timedelta(minutes=15)

EXISTING_PREFLIGHT_GATES = (
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

CLOUD_PREFLIGHT_GATES = (
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


def evaluate_cloud_preflight(
    *,
    local_preflight: Mapping[str, object],
    snapshot: CloudEvidenceSnapshot,
    expected_candidate_hash: str,
    expected_run_id: UUID,
    expected_experiment_id: UUID,
    expected_manifest_hash: str,
    expected_environment: str,
    evaluated_at: datetime,
    maximum_age: timedelta = CLOUD_PREFLIGHT_MAXIMUM_AGE,
) -> dict[str, object]:
    """Preserve all local gates and append deterministic cloud-only gates."""
    local_checks = local_preflight.get("checks")
    if not isinstance(local_checks, list) or not all(
        isinstance(check, Mapping) for check in local_checks
    ):
        raise CloudEvidenceError("local preflight checks are invalid")
    local_names = tuple(cast(Mapping[str, object], check).get("name") for check in local_checks)
    if local_names != EXISTING_PREFLIGHT_GATES:
        raise CloudEvidenceError("local preflight gate inventory differs from the contract")
    if local_preflight.get("experiment_id") != str(expected_experiment_id):
        raise CloudEvidenceError("local preflight experiment identity differs")
    if local_preflight.get("manifest_hash") != expected_manifest_hash:
        raise CloudEvidenceError("local preflight manifest identity differs")
    if local_preflight.get("safety") != {
        "paper_trading_only": True,
        "live_money": False,
    }:
        raise CloudEvidenceError("local preflight paper-only safety marker is invalid")
    validate_exact_gate_inventory(snapshot.gates, CLOUD_PREFLIGHT_GATES)
    identity_failure = snapshot_identity_failure(
        snapshot,
        expected_candidate_hash=expected_candidate_hash,
        expected_run_id=expected_run_id,
        expected_experiment_id=expected_experiment_id,
        expected_manifest_hash=expected_manifest_hash,
        expected_environment=expected_environment,
        evaluated_at=evaluated_at,
        maximum_age=maximum_age,
    )
    preserved_local_checks = [dict(cast(Mapping[str, object], check)) for check in local_checks]
    cloud_checks = [
        {
            "name": gate.name,
            "passed": gate.passed and identity_failure is None,
            "detail": identity_failure or gate.detail_code,
        }
        for gate in snapshot.gates
    ]
    checks = [*preserved_local_checks, *cloud_checks]
    base: dict[str, object] = {
        "cloud_preflight_schema_version": CLOUD_PREFLIGHT_SCHEMA_VERSION,
        "evaluated_at": evaluated_at,
        "passed": all(check.get("passed") is True for check in checks),
        "environment": expected_environment,
        "candidate_hash": expected_candidate_hash,
        "run_id": expected_run_id,
        "experiment_id": expected_experiment_id,
        "manifest_hash": expected_manifest_hash,
        "database_system_identifier_sha256": snapshot.database_system_identifier_sha256,
        "service_boot_ids": dict(snapshot.service_boot_ids),
        "source_evidence_hashes": dict(snapshot.source_evidence_hashes),
        "operation_id": snapshot.operation_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "local_preflight_report_id": local_preflight.get("report_id"),
        "safety": {"paper_trading_only": True, "live_money": False},
        "checks": checks,
    }
    normalized = to_json_data(base)
    if not isinstance(normalized, dict):
        raise TypeError("cloud preflight report must normalize to an object")
    report = cast(dict[str, object], normalized)
    report["report_id"] = content_hash(report)
    return report


def write_cloud_preflight_bundle(
    report: Mapping[str, object],
    output_directory: Path,
) -> CloudEvidenceBundlePaths:
    return write_cloud_evidence_bundle(
        report,
        output_directory,
        prefix="cloud_preflight",
        report_filename="cloud-preflight.json",
        bundle_schema_name="cloud_preflight_bundle_schema_version",
    )


def load_verified_cloud_preflight(
    directory: Path,
) -> tuple[dict[str, object], bool]:
    report, verified = load_verified_cloud_evidence(
        directory,
        report_filename="cloud-preflight.json",
    )
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        return report, False
    names = tuple(cast(Mapping[str, object], check).get("name") for check in checks)
    return report, (
        verified
        and report.get("cloud_preflight_schema_version") == CLOUD_PREFLIGHT_SCHEMA_VERSION
        and names == (*EXISTING_PREFLIGHT_GATES, *CLOUD_PREFLIGHT_GATES)
    )
