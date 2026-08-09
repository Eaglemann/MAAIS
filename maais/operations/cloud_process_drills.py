"""Read-only verdicts for explicitly authorized Railway qualification drills."""

from __future__ import annotations

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
CLOUD_PROCESS_DRILL_SCHEMA_VERSION = 1
CLOUD_PROCESS_DRILL_MAXIMUM_AGE = timedelta(hours=24)

CLOUD_DRILLS = (
    "mission_control_replacement",
    "worker_replacement_lease_takeover",
    "operations_daily_close_replacement",
    "database_interruption_fail_closed",
    "railway_artifact_target_failure",
    "worm_artifact_target_failure",
    "sentry_outage_fallback",
    "backup_restore",
)

_REPLACEMENT_DRILLS = frozenset(CLOUD_DRILLS[:3])


@dataclass(frozen=True, slots=True)
class CloudDrillObservation:
    name: str
    started_at: datetime
    action_at: datetime
    recovered_at: datetime
    before_boot_id: UUID
    after_boot_id: UUID
    before_lease_epoch: int
    after_lease_epoch: int
    duplicate_decisions: int
    duplicate_orders: int
    duplicate_fills: int
    duplicate_counterfactuals: int
    daily_report_count: int
    backup_count: int
    failed_closed: bool
    incident_recorded: bool
    alert_recorded: bool
    idempotent_retry: bool
    reconciled: bool
    source_hash: str

    def __post_init__(self) -> None:
        if self.name not in CLOUD_DRILLS:
            raise CloudEvidenceError("cloud drill name is unknown")
        for value in (self.started_at, self.action_at, self.recovered_at):
            _require_utc(value)
        if not self.started_at < self.action_at < self.recovered_at:
            raise CloudEvidenceError("cloud drill timestamps must be strictly ordered")
        if self.before_boot_id.int == 0 or self.after_boot_id.int == 0:
            raise CloudEvidenceError("cloud drill boot IDs must be non-nil")
        for value in (
            self.before_lease_epoch,
            self.after_lease_epoch,
            self.duplicate_decisions,
            self.duplicate_orders,
            self.duplicate_fills,
            self.duplicate_counterfactuals,
            self.daily_report_count,
            self.backup_count,
        ):
            if type(value) is not int or value < 0:
                raise CloudEvidenceError("cloud drill counters must be nonnegative integers")
        for value in (
            self.failed_closed,
            self.incident_recorded,
            self.alert_recorded,
            self.idempotent_retry,
            self.reconciled,
        ):
            if type(value) is not bool:
                raise CloudEvidenceError("cloud drill outcomes must be booleans")
        try:
            validate_sha256(self.source_hash)
        except ValueError as error:
            raise CloudEvidenceError("cloud drill source hash is invalid") from error

    def to_dict(self) -> dict[str, object]:
        value = to_json_data(
            {
                "name": self.name,
                "started_at": self.started_at,
                "action_at": self.action_at,
                "recovered_at": self.recovered_at,
                "before_boot_id": self.before_boot_id,
                "after_boot_id": self.after_boot_id,
                "before_lease_epoch": self.before_lease_epoch,
                "after_lease_epoch": self.after_lease_epoch,
                "duplicate_decisions": self.duplicate_decisions,
                "duplicate_orders": self.duplicate_orders,
                "duplicate_fills": self.duplicate_fills,
                "duplicate_counterfactuals": self.duplicate_counterfactuals,
                "daily_report_count": self.daily_report_count,
                "backup_count": self.backup_count,
                "failed_closed": self.failed_closed,
                "incident_recorded": self.incident_recorded,
                "alert_recorded": self.alert_recorded,
                "idempotent_retry": self.idempotent_retry,
                "reconciled": self.reconciled,
                "source_hash": self.source_hash,
            }
        )
        if not isinstance(value, dict):
            raise TypeError("cloud drill observation must normalize to an object")
        return cast(dict[str, object], value)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CloudDrillObservation:
        expected = {
            "name",
            "started_at",
            "action_at",
            "recovered_at",
            "before_boot_id",
            "after_boot_id",
            "before_lease_epoch",
            "after_lease_epoch",
            "duplicate_decisions",
            "duplicate_orders",
            "duplicate_fills",
            "duplicate_counterfactuals",
            "daily_report_count",
            "backup_count",
            "failed_closed",
            "incident_recorded",
            "alert_recorded",
            "idempotent_retry",
            "reconciled",
            "source_hash",
        }
        if set(value) != expected:
            raise CloudEvidenceError("cloud drill observation fields are not exact")
        try:
            return cls(
                name=_string(value, "name"),
                started_at=_parse_utc(value.get("started_at")),
                action_at=_parse_utc(value.get("action_at")),
                recovered_at=_parse_utc(value.get("recovered_at")),
                before_boot_id=UUID(_string(value, "before_boot_id")),
                after_boot_id=UUID(_string(value, "after_boot_id")),
                before_lease_epoch=_integer(value, "before_lease_epoch"),
                after_lease_epoch=_integer(value, "after_lease_epoch"),
                duplicate_decisions=_integer(value, "duplicate_decisions"),
                duplicate_orders=_integer(value, "duplicate_orders"),
                duplicate_fills=_integer(value, "duplicate_fills"),
                duplicate_counterfactuals=_integer(value, "duplicate_counterfactuals"),
                daily_report_count=_integer(value, "daily_report_count"),
                backup_count=_integer(value, "backup_count"),
                failed_closed=_boolean(value, "failed_closed"),
                incident_recorded=_boolean(value, "incident_recorded"),
                alert_recorded=_boolean(value, "alert_recorded"),
                idempotent_retry=_boolean(value, "idempotent_retry"),
                reconciled=_boolean(value, "reconciled"),
                source_hash=_string(value, "source_hash"),
            )
        except CloudEvidenceError:
            raise
        except ValueError as error:
            raise CloudEvidenceError("cloud drill observation identity is invalid") from error


@dataclass(frozen=True, slots=True)
class CloudProcessDrillSnapshot:
    schema_version: int
    operation_id: UUID
    environment: str
    candidate_hash: str
    run_id: UUID
    experiment_id: UUID
    manifest_hash: str
    captured_at: datetime
    observations: tuple[CloudDrillObservation, ...]
    snapshot_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != CLOUD_PROCESS_DRILL_SCHEMA_VERSION:
            raise CloudEvidenceError("cloud process drill schema version is unsupported")
        if self.environment not in {"qualification", "production"}:
            raise CloudEvidenceError("cloud process drill environment is invalid")
        for digest in (self.candidate_hash, self.manifest_hash, self.snapshot_hash):
            try:
                validate_sha256(digest)
            except ValueError as error:
                raise CloudEvidenceError("cloud process drill digest is invalid") from error
        for identifier in (self.operation_id, self.run_id, self.experiment_id):
            if identifier.int == 0:
                raise CloudEvidenceError("cloud process drill UUID must be non-nil")
        _require_utc(self.captured_at)
        if tuple(item.name for item in self.observations) != CLOUD_DRILLS:
            raise CloudEvidenceError("cloud drill inventory differs from the required contract")
        for previous, current in zip(self.observations, self.observations[1:], strict=False):
            if current.started_at <= previous.recovered_at:
                raise CloudEvidenceError("cloud drills must be strictly ordered and nonoverlapping")
        if self.observations and self.captured_at < self.observations[-1].recovered_at:
            raise CloudEvidenceError("cloud drill snapshot was captured before recovery completed")

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
        captured_at: datetime,
        observations: Sequence[CloudDrillObservation],
    ) -> CloudProcessDrillSnapshot:
        base = {
            "schema_version": CLOUD_PROCESS_DRILL_SCHEMA_VERSION,
            "operation_id": operation_id,
            "environment": environment,
            "candidate_hash": candidate_hash,
            "run_id": run_id,
            "experiment_id": experiment_id,
            "manifest_hash": manifest_hash,
            "captured_at": captured_at,
            "observations": [item.to_dict() for item in observations],
        }
        normalized = to_json_data(base)
        if not isinstance(normalized, dict):
            raise TypeError("cloud process drill snapshot must normalize to an object")
        normalized["snapshot_hash"] = content_hash(normalized)
        return cls.from_dict(normalized)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CloudProcessDrillSnapshot:
        expected = {
            "schema_version",
            "operation_id",
            "environment",
            "candidate_hash",
            "run_id",
            "experiment_id",
            "manifest_hash",
            "captured_at",
            "observations",
            "snapshot_hash",
        }
        if set(value) != expected:
            raise CloudEvidenceError("cloud process drill snapshot fields are not exact")
        without_hash = {key: item for key, item in value.items() if key != "snapshot_hash"}
        if value.get("snapshot_hash") != content_hash(without_hash):
            raise CloudEvidenceError("cloud process drill snapshot hash is invalid")
        observations = value.get("observations")
        if not isinstance(observations, list) or not all(
            isinstance(item, Mapping) for item in observations
        ):
            raise CloudEvidenceError("cloud process drill observations are invalid")
        try:
            schema_version = _integer(value, "schema_version")
            return cls(
                schema_version=schema_version,
                operation_id=UUID(_string(value, "operation_id")),
                environment=_string(value, "environment"),
                candidate_hash=_string(value, "candidate_hash"),
                run_id=UUID(_string(value, "run_id")),
                experiment_id=UUID(_string(value, "experiment_id")),
                manifest_hash=_string(value, "manifest_hash"),
                captured_at=_parse_utc(value.get("captured_at")),
                observations=tuple(
                    CloudDrillObservation.from_dict(cast(Mapping[str, object], item))
                    for item in observations
                ),
                snapshot_hash=_string(value, "snapshot_hash"),
            )
        except CloudEvidenceError:
            raise
        except ValueError as error:
            raise CloudEvidenceError("cloud process drill snapshot identity is invalid") from error

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
                "captured_at": self.captured_at,
                "observations": [item.to_dict() for item in self.observations],
                "snapshot_hash": self.snapshot_hash,
            }
        )
        if not isinstance(value, dict):
            raise TypeError("cloud process drill snapshot must normalize to an object")
        return cast(dict[str, object], value)


def evaluate_cloud_process_drills(
    snapshot: CloudProcessDrillSnapshot,
    *,
    expected_candidate_hash: str,
    expected_run_id: UUID,
    expected_experiment_id: UUID,
    expected_manifest_hash: str,
    expected_environment: str,
    evaluated_at: datetime,
    maximum_age: timedelta = CLOUD_PROCESS_DRILL_MAXIMUM_AGE,
) -> dict[str, object]:
    _require_utc(evaluated_at)
    identity_failure = _identity_failure(
        snapshot,
        expected_candidate_hash=expected_candidate_hash,
        expected_run_id=expected_run_id,
        expected_experiment_id=expected_experiment_id,
        expected_manifest_hash=expected_manifest_hash,
        expected_environment=expected_environment,
        evaluated_at=evaluated_at,
        maximum_age=maximum_age,
    )
    checks = []
    for observation in snapshot.observations:
        detail = identity_failure or _drill_failure(observation)
        checks.append(
            {
                "name": observation.name,
                "passed": detail is None,
                "detail": detail or "verified",
                "source_hash": observation.source_hash,
            }
        )
    base = {
        "cloud_process_drill_schema_version": CLOUD_PROCESS_DRILL_SCHEMA_VERSION,
        "evaluated_at": evaluated_at,
        "passed": all(check["passed"] is True for check in checks),
        "environment": expected_environment,
        "candidate_hash": expected_candidate_hash,
        "run_id": expected_run_id,
        "experiment_id": expected_experiment_id,
        "manifest_hash": expected_manifest_hash,
        "operation_id": snapshot.operation_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "safety": {"paper_trading_only": True, "live_money": False},
        "checks": checks,
    }
    normalized = to_json_data(base)
    if not isinstance(normalized, dict):
        raise TypeError("cloud process drill report must normalize to an object")
    report = cast(dict[str, object], normalized)
    report["report_id"] = content_hash(report)
    return report


def write_cloud_process_drill_bundle(
    report: Mapping[str, object],
    output_directory: Path,
) -> CloudEvidenceBundlePaths:
    return write_cloud_evidence_bundle(
        report,
        output_directory,
        prefix="cloud_process_drills",
        report_filename="cloud-process-drills.json",
        bundle_schema_name="cloud_process_drill_bundle_schema_version",
    )


def load_verified_cloud_process_drills(
    directory: Path,
) -> tuple[dict[str, object], bool]:
    report, verified = load_verified_cloud_evidence(
        directory,
        report_filename="cloud-process-drills.json",
    )
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        return report, False
    names = tuple(cast(Mapping[str, object], check).get("name") for check in checks)
    return report, (
        verified
        and report.get("cloud_process_drill_schema_version") == CLOUD_PROCESS_DRILL_SCHEMA_VERSION
        and names == CLOUD_DRILLS
    )


def _drill_failure(observation: CloudDrillObservation) -> str | None:
    if any(
        value != 0
        for value in (
            observation.duplicate_decisions,
            observation.duplicate_orders,
            observation.duplicate_fills,
            observation.duplicate_counterfactuals,
        )
    ):
        return "duplicate_trade_state"
    if observation.name in _REPLACEMENT_DRILLS and (
        observation.before_boot_id == observation.after_boot_id
    ):
        return "service_not_replaced"
    if observation.name == "worker_replacement_lease_takeover" and (
        observation.after_lease_epoch <= observation.before_lease_epoch
    ):
        return "lease_epoch_not_increased"
    if observation.name == "operations_daily_close_replacement" and (
        observation.daily_report_count != 1 or observation.backup_count != 1
    ):
        return "daily_close_not_exactly_once"
    if not observation.failed_closed:
        return "failure_not_fail_closed"
    if not observation.incident_recorded:
        return "incident_missing"
    if not observation.alert_recorded:
        return "alert_missing"
    if not observation.idempotent_retry:
        return "retry_not_idempotent"
    if not observation.reconciled:
        return "reconciliation_failed"
    return None


def _identity_failure(
    snapshot: CloudProcessDrillSnapshot,
    *,
    expected_candidate_hash: str,
    expected_run_id: UUID,
    expected_experiment_id: UUID,
    expected_manifest_hash: str,
    expected_environment: str,
    evaluated_at: datetime,
    maximum_age: timedelta,
) -> str | None:
    if maximum_age <= timedelta(0):
        raise ValueError("cloud process drill maximum age must be positive")
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
    if age > maximum_age:
        return "snapshot_stale"
    return None


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise CloudEvidenceError(f"cloud drill {key} must be a string")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise CloudEvidenceError(f"cloud drill {key} must be an integer")
    return item


def _boolean(value: Mapping[str, object], key: str) -> bool:
    item = value.get(key)
    if type(item) is not bool:
        raise CloudEvidenceError(f"cloud drill {key} must be a boolean")
    return item


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise CloudEvidenceError("cloud drill timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CloudEvidenceError("cloud drill timestamp is invalid") from error
    _require_utc(parsed)
    return parsed.astimezone(UTC)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CloudEvidenceError("cloud process drill timestamp must be UTC-aware")
