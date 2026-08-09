"""Immutable Railway extension to the existing 24-hour soak verdict."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

from maais.artifacts.models import validate_sha256
from maais.domain.json import content_hash, to_json_data
from maais.operations.cloud_evidence import (
    CloudEvidenceBundlePaths,
    CloudEvidenceError,
    load_verified_cloud_evidence,
    write_cloud_evidence_bundle,
)

UTC = timezone.utc
CLOUD_SOAK_SCHEMA_VERSION = 1
CLOUD_SOAK_DURATION = timedelta(hours=24)
CLOUD_SOAK_SNAPSHOT_MAXIMUM_AGE = timedelta(minutes=5)
CLOUD_HEALTH_MAXIMUM_LAG = timedelta(seconds=90)
_SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,95}$")

EXISTING_SOAK_GATES = (
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

CLOUD_SOAK_GATES = (
    "cloud_identity_continuity",
    "external_monitoring",
    "audit_chain_integrity",
    "dual_store_artifacts",
    "backup_restore_evidence",
    "operator_auth_health",
    "resource_cost_headroom",
)

REQUIRED_CLOUD_ROLES = ("operations", "verifier", "web", "worker")


@dataclass(frozen=True, slots=True)
class CloudSoakSnapshot:
    schema_version: int
    operation_id: UUID
    environment: str
    candidate_hash: str
    run_id: UUID
    experiment_id: UUID
    manifest_hash: str
    database_system_identifier_sha256: str
    activated_at: datetime
    captured_at: datetime
    service_boot_ids: tuple[tuple[str, tuple[UUID, ...]], ...]
    interruption_events: tuple[str, ...]
    configuration_event_count: int
    health_sample_count: int
    expected_health_sample_count: int
    newest_health_at: datetime
    external_monitor_sample_count: int
    expected_external_monitor_sample_count: int
    sentry_gap_count: int
    audit_chain_valid: bool
    dual_store_artifacts_valid: bool
    backup_restore_verified: bool
    auth_probe_valid: bool
    resource_cost_headroom: bool
    source_evidence_hashes: tuple[tuple[str, str], ...]
    snapshot_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != CLOUD_SOAK_SCHEMA_VERSION:
            raise CloudEvidenceError("cloud soak schema version is unsupported")
        if self.environment not in {"qualification", "production"}:
            raise CloudEvidenceError("cloud soak environment is invalid")
        for digest in (
            self.candidate_hash,
            self.manifest_hash,
            self.database_system_identifier_sha256,
            self.snapshot_hash,
        ):
            try:
                validate_sha256(digest)
            except ValueError as error:
                raise CloudEvidenceError("cloud soak digest is invalid") from error
        for identifier in (self.operation_id, self.run_id, self.experiment_id):
            if identifier.int == 0:
                raise CloudEvidenceError("cloud soak UUID must be non-nil")
        for timestamp in (self.activated_at, self.captured_at, self.newest_health_at):
            _require_utc(timestamp)
        if self.captured_at < self.activated_at:
            raise CloudEvidenceError("cloud soak snapshot predates activation")
        role_names = tuple(name for name, _ in self.service_boot_ids)
        if role_names != REQUIRED_CLOUD_ROLES:
            raise CloudEvidenceError("cloud soak service role inventory is not exact")
        for _, boot_ids in self.service_boot_ids:
            if not boot_ids or any(identifier.int == 0 for identifier in boot_ids):
                raise CloudEvidenceError("cloud soak service boot history is invalid")
        if any(not _SAFE_EVENT.fullmatch(event) for event in self.interruption_events):
            raise CloudEvidenceError("cloud soak interruption event is invalid")
        for value in (
            self.configuration_event_count,
            self.health_sample_count,
            self.expected_health_sample_count,
            self.external_monitor_sample_count,
            self.expected_external_monitor_sample_count,
            self.sentry_gap_count,
        ):
            if type(value) is not int or value < 0:
                raise CloudEvidenceError("cloud soak counters must be nonnegative integers")
        for value in (
            self.audit_chain_valid,
            self.dual_store_artifacts_valid,
            self.backup_restore_verified,
            self.auth_probe_valid,
            self.resource_cost_headroom,
        ):
            if type(value) is not bool:
                raise CloudEvidenceError("cloud soak outcomes must be booleans")
        source_names = tuple(name for name, _ in self.source_evidence_hashes)
        if (
            not source_names
            or source_names != tuple(sorted(source_names))
            or len(set(source_names)) != len(source_names)
        ):
            raise CloudEvidenceError("cloud soak source inventory is invalid")
        for name, digest in self.source_evidence_hashes:
            if not _SAFE_EVENT.fullmatch(name):
                raise CloudEvidenceError("cloud soak source name is invalid")
            try:
                validate_sha256(digest)
            except ValueError as error:
                raise CloudEvidenceError("cloud soak source hash is invalid") from error

    @classmethod
    def create(
        cls,
        *,
        operation_id: UUID,
        environment: str,
        candidate_hash: str,
        run_id: UUID,
        experiment_id: UUID,
        manifest_hash: str,
        database_system_identifier_sha256: str,
        activated_at: datetime,
        captured_at: datetime,
        service_boot_ids: Mapping[str, Sequence[UUID]],
        interruption_events: Sequence[str],
        configuration_event_count: int,
        health_sample_count: int,
        expected_health_sample_count: int,
        newest_health_at: datetime,
        external_monitor_sample_count: int,
        expected_external_monitor_sample_count: int,
        sentry_gap_count: int,
        audit_chain_valid: bool,
        dual_store_artifacts_valid: bool,
        backup_restore_verified: bool,
        auth_probe_valid: bool,
        resource_cost_headroom: bool,
        source_evidence_hashes: Mapping[str, str],
    ) -> CloudSoakSnapshot:
        base = {
            "schema_version": CLOUD_SOAK_SCHEMA_VERSION,
            "operation_id": operation_id,
            "environment": environment,
            "candidate_hash": candidate_hash,
            "run_id": run_id,
            "experiment_id": experiment_id,
            "manifest_hash": manifest_hash,
            "database_system_identifier_sha256": database_system_identifier_sha256,
            "activated_at": activated_at,
            "captured_at": captured_at,
            "service_boot_ids": {
                name: list(identifiers) for name, identifiers in sorted(service_boot_ids.items())
            },
            "interruption_events": list(interruption_events),
            "configuration_event_count": configuration_event_count,
            "health_sample_count": health_sample_count,
            "expected_health_sample_count": expected_health_sample_count,
            "newest_health_at": newest_health_at,
            "external_monitor_sample_count": external_monitor_sample_count,
            "expected_external_monitor_sample_count": expected_external_monitor_sample_count,
            "sentry_gap_count": sentry_gap_count,
            "audit_chain_valid": audit_chain_valid,
            "dual_store_artifacts_valid": dual_store_artifacts_valid,
            "backup_restore_verified": backup_restore_verified,
            "auth_probe_valid": auth_probe_valid,
            "resource_cost_headroom": resource_cost_headroom,
            "source_evidence_hashes": dict(sorted(source_evidence_hashes.items())),
        }
        normalized = to_json_data(base)
        if not isinstance(normalized, dict):
            raise TypeError("cloud soak snapshot must normalize to an object")
        normalized["snapshot_hash"] = content_hash(normalized)
        return cls.from_dict(normalized)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CloudSoakSnapshot:
        expected = {
            "schema_version",
            "operation_id",
            "environment",
            "candidate_hash",
            "run_id",
            "experiment_id",
            "manifest_hash",
            "database_system_identifier_sha256",
            "activated_at",
            "captured_at",
            "service_boot_ids",
            "interruption_events",
            "configuration_event_count",
            "health_sample_count",
            "expected_health_sample_count",
            "newest_health_at",
            "external_monitor_sample_count",
            "expected_external_monitor_sample_count",
            "sentry_gap_count",
            "audit_chain_valid",
            "dual_store_artifacts_valid",
            "backup_restore_verified",
            "auth_probe_valid",
            "resource_cost_headroom",
            "source_evidence_hashes",
            "snapshot_hash",
        }
        if set(value) != expected:
            raise CloudEvidenceError("cloud soak snapshot fields are not exact")
        without_hash = {key: item for key, item in value.items() if key != "snapshot_hash"}
        if value.get("snapshot_hash") != content_hash(without_hash):
            raise CloudEvidenceError("cloud soak snapshot hash is invalid")
        raw_boot_ids = value.get("service_boot_ids")
        raw_events = value.get("interruption_events")
        raw_sources = value.get("source_evidence_hashes")
        if not isinstance(raw_boot_ids, Mapping) or not all(
            isinstance(name, str) and isinstance(ids, list) for name, ids in raw_boot_ids.items()
        ):
            raise CloudEvidenceError("cloud soak boot history is invalid")
        if not isinstance(raw_events, list) or not all(
            isinstance(event, str) for event in raw_events
        ):
            raise CloudEvidenceError("cloud soak interruption history is invalid")
        if not isinstance(raw_sources, Mapping) or not all(
            isinstance(name, str) and isinstance(digest, str)
            for name, digest in raw_sources.items()
        ):
            raise CloudEvidenceError("cloud soak source hashes are invalid")
        try:
            return cls(
                schema_version=_integer(value, "schema_version"),
                operation_id=UUID(_string(value, "operation_id")),
                environment=_string(value, "environment"),
                candidate_hash=_string(value, "candidate_hash"),
                run_id=UUID(_string(value, "run_id")),
                experiment_id=UUID(_string(value, "experiment_id")),
                manifest_hash=_string(value, "manifest_hash"),
                database_system_identifier_sha256=_string(
                    value, "database_system_identifier_sha256"
                ),
                activated_at=_parse_utc(value.get("activated_at")),
                captured_at=_parse_utc(value.get("captured_at")),
                service_boot_ids=tuple(
                    sorted(
                        (
                            cast(str, name),
                            tuple(UUID(str(identifier)) for identifier in cast(list[object], ids)),
                        )
                        for name, ids in raw_boot_ids.items()
                    )
                ),
                interruption_events=tuple(cast(list[str], raw_events)),
                configuration_event_count=_integer(value, "configuration_event_count"),
                health_sample_count=_integer(value, "health_sample_count"),
                expected_health_sample_count=_integer(value, "expected_health_sample_count"),
                newest_health_at=_parse_utc(value.get("newest_health_at")),
                external_monitor_sample_count=_integer(value, "external_monitor_sample_count"),
                expected_external_monitor_sample_count=_integer(
                    value, "expected_external_monitor_sample_count"
                ),
                sentry_gap_count=_integer(value, "sentry_gap_count"),
                audit_chain_valid=_boolean(value, "audit_chain_valid"),
                dual_store_artifacts_valid=_boolean(value, "dual_store_artifacts_valid"),
                backup_restore_verified=_boolean(value, "backup_restore_verified"),
                auth_probe_valid=_boolean(value, "auth_probe_valid"),
                resource_cost_headroom=_boolean(value, "resource_cost_headroom"),
                source_evidence_hashes=tuple(
                    sorted(
                        (cast(str, name), cast(str, digest)) for name, digest in raw_sources.items()
                    )
                ),
                snapshot_hash=_string(value, "snapshot_hash"),
            )
        except CloudEvidenceError:
            raise
        except ValueError as error:
            raise CloudEvidenceError("cloud soak snapshot identity is invalid") from error

    def to_dict(self) -> dict[str, object]:
        value = to_json_data(
            {
                "schema_version": self.schema_version,
                "operation_id": self.operation_id,
                "environment": self.environment,
                "candidate_hash": self.candidate_hash,
                "run_id": self.run_id,
                "experiment_id": self.experiment_id,
                "manifest_hash": self.manifest_hash,
                "database_system_identifier_sha256": (self.database_system_identifier_sha256),
                "activated_at": self.activated_at,
                "captured_at": self.captured_at,
                "service_boot_ids": {
                    name: list(identifiers) for name, identifiers in self.service_boot_ids
                },
                "interruption_events": list(self.interruption_events),
                "configuration_event_count": self.configuration_event_count,
                "health_sample_count": self.health_sample_count,
                "expected_health_sample_count": self.expected_health_sample_count,
                "newest_health_at": self.newest_health_at,
                "external_monitor_sample_count": self.external_monitor_sample_count,
                "expected_external_monitor_sample_count": (
                    self.expected_external_monitor_sample_count
                ),
                "sentry_gap_count": self.sentry_gap_count,
                "audit_chain_valid": self.audit_chain_valid,
                "dual_store_artifacts_valid": self.dual_store_artifacts_valid,
                "backup_restore_verified": self.backup_restore_verified,
                "auth_probe_valid": self.auth_probe_valid,
                "resource_cost_headroom": self.resource_cost_headroom,
                "source_evidence_hashes": dict(self.source_evidence_hashes),
                "snapshot_hash": self.snapshot_hash,
            }
        )
        if not isinstance(value, dict):
            raise TypeError("cloud soak snapshot must normalize to an object")
        return cast(dict[str, object], value)


def evaluate_cloud_soak_readiness(
    *,
    local_soak: Mapping[str, object],
    snapshot: CloudSoakSnapshot,
    expected_candidate_hash: str,
    expected_run_id: UUID,
    expected_experiment_id: UUID,
    expected_manifest_hash: str,
    expected_environment: str,
    evaluated_at: datetime,
    maximum_snapshot_age: timedelta = CLOUD_SOAK_SNAPSHOT_MAXIMUM_AGE,
) -> dict[str, object]:
    _require_utc(evaluated_at)
    if evaluated_at - snapshot.activated_at < CLOUD_SOAK_DURATION:
        raise CloudEvidenceError("cloud soak verdict refused before 24 hours")
    local_checks = local_soak.get("checks")
    if not isinstance(local_checks, list) or not all(
        isinstance(check, Mapping) for check in local_checks
    ):
        raise CloudEvidenceError("local soak checks are invalid")
    names = tuple(cast(Mapping[str, object], check).get("name") for check in local_checks)
    if names != EXISTING_SOAK_GATES:
        raise CloudEvidenceError("local soak gate inventory differs from the contract")
    experiment = local_soak.get("experiment")
    if not isinstance(experiment, Mapping):
        raise CloudEvidenceError("local soak experiment identity is invalid")
    if experiment.get("id") != str(expected_experiment_id):
        raise CloudEvidenceError("local soak experiment identity differs")
    if experiment.get("manifest_hash") != expected_manifest_hash:
        raise CloudEvidenceError("local soak manifest identity differs")
    if local_soak.get("safety") != {"paper_trading_only": True, "live_money": False}:
        raise CloudEvidenceError("local soak paper-only marker is invalid")
    identity_detail = _identity_continuity_detail(
        snapshot,
        expected_candidate_hash=expected_candidate_hash,
        expected_run_id=expected_run_id,
        expected_experiment_id=expected_experiment_id,
        expected_manifest_hash=expected_manifest_hash,
        expected_environment=expected_environment,
        evaluated_at=evaluated_at,
        maximum_snapshot_age=maximum_snapshot_age,
    )
    cloud_details = {
        "cloud_identity_continuity": identity_detail,
        "external_monitoring": _monitoring_detail(snapshot, evaluated_at),
        "audit_chain_integrity": (None if snapshot.audit_chain_valid else "audit_chain_invalid"),
        "dual_store_artifacts": (
            None if snapshot.dual_store_artifacts_valid else "artifact_replication_invalid"
        ),
        "backup_restore_evidence": (
            None if snapshot.backup_restore_verified else "backup_restore_unverified"
        ),
        "operator_auth_health": None if snapshot.auth_probe_valid else "auth_probe_failed",
        "resource_cost_headroom": (
            None if snapshot.resource_cost_headroom else "resource_or_cost_cutoff"
        ),
    }
    cloud_checks = [
        {
            "name": name,
            "passed": cloud_details[name] is None,
            "detail": cloud_details[name] or "verified",
        }
        for name in CLOUD_SOAK_GATES
    ]
    checks = [
        *(dict(cast(Mapping[str, object], check)) for check in local_checks),
        *cloud_checks,
    ]
    passed = all(check.get("passed") is True for check in checks)
    base = {
        "cloud_soak_schema_version": CLOUD_SOAK_SCHEMA_VERSION,
        "evaluated_at": evaluated_at,
        "passed": passed,
        "verdict": "ready_for_seven_day_paper_test" if passed else "not_ready",
        "environment": expected_environment,
        "candidate_hash": expected_candidate_hash,
        "run_id": expected_run_id,
        "experiment_id": expected_experiment_id,
        "manifest_hash": expected_manifest_hash,
        "database_system_identifier_sha256": snapshot.database_system_identifier_sha256,
        "operation_id": snapshot.operation_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "source_evidence_hashes": dict(snapshot.source_evidence_hashes),
        "service_boot_ids": {
            name: list(identifiers) for name, identifiers in snapshot.service_boot_ids
        },
        "local_soak_report_id": local_soak.get("report_id"),
        "decision_coverage": local_soak.get("decision_coverage"),
        "safety": {"paper_trading_only": True, "live_money": False},
        "checks": checks,
    }
    normalized = to_json_data(base)
    if not isinstance(normalized, dict):
        raise TypeError("cloud soak report must normalize to an object")
    report = cast(dict[str, object], normalized)
    report["report_id"] = content_hash(report)
    return report


def write_cloud_soak_readiness_bundle(
    report: Mapping[str, object],
    output_directory: Path,
) -> CloudEvidenceBundlePaths:
    return write_cloud_evidence_bundle(
        report,
        output_directory,
        prefix="cloud_soak",
        report_filename="cloud-soak-readiness.json",
        bundle_schema_name="cloud_soak_bundle_schema_version",
    )


def load_verified_cloud_soak_readiness(
    directory: Path,
) -> tuple[dict[str, object], bool]:
    report, verified = load_verified_cloud_evidence(
        directory,
        report_filename="cloud-soak-readiness.json",
    )
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        return report, False
    names = tuple(cast(Mapping[str, object], check).get("name") for check in checks)
    return report, (
        verified
        and report.get("cloud_soak_schema_version") == CLOUD_SOAK_SCHEMA_VERSION
        and names == (*EXISTING_SOAK_GATES, *CLOUD_SOAK_GATES)
    )


def _identity_continuity_detail(
    snapshot: CloudSoakSnapshot,
    *,
    expected_candidate_hash: str,
    expected_run_id: UUID,
    expected_experiment_id: UUID,
    expected_manifest_hash: str,
    expected_environment: str,
    evaluated_at: datetime,
    maximum_snapshot_age: timedelta,
) -> str | None:
    if snapshot.candidate_hash != expected_candidate_hash:
        return "candidate_mismatch"
    if snapshot.run_id != expected_run_id:
        return "run_mismatch"
    if snapshot.experiment_id != expected_experiment_id:
        return "experiment_mismatch"
    if snapshot.manifest_hash != expected_manifest_hash:
        return "manifest_mismatch"
    if snapshot.environment != expected_environment:
        return "environment_mismatch"
    age = evaluated_at - snapshot.captured_at
    if age < timedelta(0):
        return "snapshot_from_future"
    if age > maximum_snapshot_age:
        return "snapshot_stale"
    if any(len(boot_ids) != 1 for _, boot_ids in snapshot.service_boot_ids):
        return "service_boot_changed"
    if snapshot.interruption_events:
        return "interruption_recorded"
    if snapshot.configuration_event_count:
        return "configuration_changed"
    return None


def _monitoring_detail(snapshot: CloudSoakSnapshot, evaluated_at: datetime) -> str | None:
    if snapshot.health_sample_count < snapshot.expected_health_sample_count:
        return "health_samples_incomplete"
    if snapshot.external_monitor_sample_count < snapshot.expected_external_monitor_sample_count:
        return "monitor_samples_incomplete"
    lag = evaluated_at - snapshot.newest_health_at
    if lag < timedelta(0) or lag > CLOUD_HEALTH_MAXIMUM_LAG:
        return "health_sample_stale"
    if snapshot.sentry_gap_count:
        return "sentry_delivery_gap"
    return None


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise CloudEvidenceError(f"cloud soak {key} must be a string")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise CloudEvidenceError(f"cloud soak {key} must be an integer")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if type(item) is not bool:
        raise CloudEvidenceError(f"cloud soak {key} must be a boolean")
    return item


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise CloudEvidenceError("cloud soak timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CloudEvidenceError("cloud soak timestamp is invalid") from error
    _require_utc(parsed)
    return parsed.astimezone(UTC)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CloudEvidenceError("cloud soak timestamp must be UTC-aware")
