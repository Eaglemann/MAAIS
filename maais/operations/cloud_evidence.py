"""Immutable, secret-free evidence captured from mutable cloud providers."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

from maais.artifacts.bundles import validate_bundle
from maais.artifacts.models import validate_sha256
from maais.domain.json import content_hash, to_json_data

UTC = timezone.utc
CLOUD_EVIDENCE_SCHEMA_VERSION = 1
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class CloudEvidenceError(ValueError):
    """Raised when frozen cloud evidence is incomplete, unsafe, or unverifiable."""


@dataclass(frozen=True, slots=True)
class CloudGateEvidence:
    name: str
    passed: bool
    detail_code: str

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.name):
            raise CloudEvidenceError("cloud gate name is invalid")
        if type(self.passed) is not bool:
            raise CloudEvidenceError("cloud gate passed value must be boolean")
        if not _SAFE_CODE.fullmatch(self.detail_code):
            raise CloudEvidenceError("cloud gate detail code is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail_code": self.detail_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CloudGateEvidence:
        if set(value) != {"name", "passed", "detail_code"}:
            raise CloudEvidenceError("cloud gate evidence fields are not exact")
        name = value.get("name")
        passed = value.get("passed")
        detail_code = value.get("detail_code")
        if (
            not isinstance(name, str)
            or type(passed) is not bool
            or not isinstance(detail_code, str)
        ):
            raise CloudEvidenceError("cloud gate evidence values are invalid")
        return cls(name=name, passed=passed, detail_code=detail_code)


@dataclass(frozen=True, slots=True)
class CloudEvidenceSnapshot:
    schema_version: int
    operation_id: UUID
    environment: str
    candidate_hash: str
    run_id: UUID
    experiment_id: UUID
    manifest_hash: str
    database_system_identifier_sha256: str
    captured_at: datetime
    service_boot_ids: tuple[tuple[str, UUID], ...]
    source_evidence_hashes: tuple[tuple[str, str], ...]
    gates: tuple[CloudGateEvidence, ...]
    snapshot_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != CLOUD_EVIDENCE_SCHEMA_VERSION:
            raise CloudEvidenceError("cloud snapshot schema version is unsupported")
        if self.environment not in {"qualification", "production"}:
            raise CloudEvidenceError("cloud snapshot environment is invalid")
        for digest in (
            self.candidate_hash,
            self.manifest_hash,
            self.database_system_identifier_sha256,
            self.snapshot_hash,
        ):
            try:
                validate_sha256(digest)
            except ValueError as error:
                raise CloudEvidenceError("cloud snapshot digest is invalid") from error
        for identifier in (self.operation_id, self.run_id, self.experiment_id):
            if identifier.int == 0:
                raise CloudEvidenceError("cloud snapshot UUID must be non-nil")
        _require_utc(self.captured_at, "cloud snapshot captured_at")
        _validate_named_uuids(self.service_boot_ids)
        _validate_named_hashes(self.source_evidence_hashes)
        if not self.gates:
            raise CloudEvidenceError("cloud snapshot must contain gate evidence")

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
        captured_at: datetime,
        service_boot_ids: Mapping[str, UUID],
        source_evidence_hashes: Mapping[str, str],
        gates: Sequence[CloudGateEvidence],
    ) -> CloudEvidenceSnapshot:
        base: dict[str, object] = {
            "schema_version": CLOUD_EVIDENCE_SCHEMA_VERSION,
            "operation_id": operation_id,
            "environment": environment,
            "candidate_hash": candidate_hash,
            "run_id": run_id,
            "experiment_id": experiment_id,
            "manifest_hash": manifest_hash,
            "database_system_identifier_sha256": database_system_identifier_sha256,
            "captured_at": captured_at,
            "service_boot_ids": {name: value for name, value in sorted(service_boot_ids.items())},
            "source_evidence_hashes": {
                name: value for name, value in sorted(source_evidence_hashes.items())
            },
            "gates": [gate.to_dict() for gate in gates],
        }
        normalized = to_json_data(base)
        if not isinstance(normalized, dict):
            raise TypeError("cloud snapshot must normalize to an object")
        normalized["snapshot_hash"] = content_hash(normalized)
        return cls.from_dict(normalized)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CloudEvidenceSnapshot:
        expected_fields = {
            "schema_version",
            "operation_id",
            "environment",
            "candidate_hash",
            "run_id",
            "experiment_id",
            "manifest_hash",
            "database_system_identifier_sha256",
            "captured_at",
            "service_boot_ids",
            "source_evidence_hashes",
            "gates",
            "snapshot_hash",
        }
        if set(value) != expected_fields:
            raise CloudEvidenceError("cloud snapshot fields are not exact")
        without_hash = {key: item for key, item in value.items() if key != "snapshot_hash"}
        if value.get("snapshot_hash") != content_hash(without_hash):
            raise CloudEvidenceError("cloud snapshot hash is invalid")
        try:
            operation_id = UUID(str(value["operation_id"]))
            run_id = UUID(str(value["run_id"]))
            experiment_id = UUID(str(value["experiment_id"]))
            captured_at = _parse_utc(value["captured_at"], "cloud snapshot captured_at")
        except (KeyError, ValueError) as error:
            raise CloudEvidenceError("cloud snapshot identity is invalid") from error
        raw_boot_ids = value.get("service_boot_ids")
        raw_sources = value.get("source_evidence_hashes")
        raw_gates = value.get("gates")
        if not isinstance(raw_boot_ids, Mapping) or not isinstance(raw_sources, Mapping):
            raise CloudEvidenceError("cloud snapshot source identity is invalid")
        if not isinstance(raw_gates, list) or not all(
            isinstance(item, Mapping) for item in raw_gates
        ):
            raise CloudEvidenceError("cloud snapshot gates are invalid")
        boot_ids: list[tuple[str, UUID]] = []
        for name, identifier in raw_boot_ids.items():
            if not isinstance(name, str):
                raise CloudEvidenceError("cloud snapshot service name is invalid")
            try:
                boot_ids.append((name, UUID(str(identifier))))
            except ValueError as error:
                raise CloudEvidenceError("cloud snapshot boot identity is invalid") from error
        sources: list[tuple[str, str]] = []
        for name, digest in raw_sources.items():
            if not isinstance(name, str) or not isinstance(digest, str):
                raise CloudEvidenceError("cloud snapshot source hash is invalid")
            sources.append((name, digest))
        schema_version = value.get("schema_version")
        environment = value.get("environment")
        candidate_hash = value.get("candidate_hash")
        manifest_hash = value.get("manifest_hash")
        database_hash = value.get("database_system_identifier_sha256")
        snapshot_hash = value.get("snapshot_hash")
        if (
            type(schema_version) is not int
            or not isinstance(environment, str)
            or not isinstance(candidate_hash, str)
            or not isinstance(manifest_hash, str)
            or not isinstance(database_hash, str)
            or not isinstance(snapshot_hash, str)
        ):
            raise CloudEvidenceError("cloud snapshot scalar values are invalid")
        return cls(
            schema_version=schema_version,
            operation_id=operation_id,
            environment=environment,
            candidate_hash=candidate_hash,
            run_id=run_id,
            experiment_id=experiment_id,
            manifest_hash=manifest_hash,
            database_system_identifier_sha256=database_hash,
            captured_at=captured_at,
            service_boot_ids=tuple(sorted(boot_ids)),
            source_evidence_hashes=tuple(sorted(sources)),
            gates=tuple(
                CloudGateEvidence.from_dict(cast(Mapping[str, object], item)) for item in raw_gates
            ),
            snapshot_hash=snapshot_hash,
        )

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
                "database_system_identifier_sha256": self.database_system_identifier_sha256,
                "captured_at": self.captured_at,
                "service_boot_ids": dict(self.service_boot_ids),
                "source_evidence_hashes": dict(self.source_evidence_hashes),
                "gates": [gate.to_dict() for gate in self.gates],
                "snapshot_hash": self.snapshot_hash,
            }
        )
        if not isinstance(value, dict):
            raise TypeError("cloud snapshot must normalize to an object")
        return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class CloudEvidenceBundlePaths:
    directory: Path
    report_path: Path
    manifest_path: Path


def write_cloud_evidence_bundle(
    report: Mapping[str, object],
    output_directory: Path,
    *,
    prefix: str,
    report_filename: str,
    bundle_schema_name: str,
) -> CloudEvidenceBundlePaths:
    report_id = report.get("report_id")
    without_id = {key: value for key, value in report.items() if key != "report_id"}
    if not isinstance(report_id, str) or report_id != content_hash(without_id):
        raise CloudEvidenceError("cloud report identity is invalid")
    if Path(report_filename).name != report_filename or not report_filename.endswith(".json"):
        raise CloudEvidenceError("cloud report filename is invalid")
    if not _SAFE_NAME.fullmatch(prefix) or not _SAFE_NAME.fullmatch(bundle_schema_name):
        raise CloudEvidenceError("cloud bundle name is invalid")
    target = output_directory / f"{prefix}-{report_id[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"cloud evidence bundle already exists: {target}")
    with tempfile.TemporaryDirectory(prefix=f".maais-{prefix}-", dir=output_directory) as tmp:
        temporary = Path(tmp)
        report_path = temporary / report_filename
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path = temporary / "bundle-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    bundle_schema_name: 1,
                    "report_id": report_id,
                    "artifacts": {
                        report_filename: {
                            "sha256": _sha256(report_path),
                            "bytes": report_path.stat().st_size,
                        }
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    paths = CloudEvidenceBundlePaths(
        directory=target,
        report_path=target / report_filename,
        manifest_path=target / "bundle-manifest.json",
    )
    validate_bundle(paths.directory, expected_report_id=report_id)
    return paths


def load_verified_cloud_evidence(
    directory: Path,
    *,
    report_filename: str,
) -> tuple[dict[str, object], bool]:
    report_path = directory / report_filename
    value = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CloudEvidenceError("cloud evidence report must contain a JSON object")
    report = cast(dict[str, object], value)
    report_id = report.get("report_id")
    if not isinstance(report_id, str):
        return report, False
    without_id = {key: item for key, item in report.items() if key != "report_id"}
    if report_id != content_hash(without_id):
        return report, False
    try:
        validate_bundle(directory, expected_report_id=report_id)
    except (OSError, ValueError):
        return report, False
    return report, True


def validate_exact_gate_inventory(
    gates: Sequence[CloudGateEvidence],
    expected: Sequence[str],
) -> None:
    names = tuple(gate.name for gate in gates)
    if names != tuple(expected):
        raise CloudEvidenceError("cloud gate inventory differs from the required contract")


def snapshot_identity_failure(
    snapshot: CloudEvidenceSnapshot,
    *,
    expected_candidate_hash: str,
    expected_run_id: UUID,
    expected_experiment_id: UUID,
    expected_manifest_hash: str,
    expected_environment: str,
    evaluated_at: datetime,
    maximum_age: timedelta,
) -> str | None:
    _require_utc(evaluated_at, "cloud evidence evaluated_at")
    if maximum_age <= timedelta(0):
        raise ValueError("cloud evidence maximum age must be positive")
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


def _validate_named_uuids(values: Sequence[tuple[str, UUID]]) -> None:
    names = tuple(name for name, _ in values)
    if tuple(sorted(names)) != names or len(set(names)) != len(names) or not names:
        raise CloudEvidenceError("cloud snapshot service boot inventory is invalid")
    for name, identifier in values:
        if not _SAFE_NAME.fullmatch(name) or identifier.int == 0:
            raise CloudEvidenceError("cloud snapshot service boot identity is invalid")


def _validate_named_hashes(values: Sequence[tuple[str, str]]) -> None:
    names = tuple(name for name, _ in values)
    if tuple(sorted(names)) != names or len(set(names)) != len(names) or not names:
        raise CloudEvidenceError("cloud snapshot source hash inventory is invalid")
    for name, digest in values:
        if not _SAFE_NAME.fullmatch(name):
            raise CloudEvidenceError("cloud snapshot source name is invalid")
        try:
            validate_sha256(digest)
        except ValueError as error:
            raise CloudEvidenceError("cloud snapshot source hash is invalid") from error


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise CloudEvidenceError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CloudEvidenceError(f"{label} is invalid") from error
    _require_utc(parsed, label)
    return parsed.astimezone(UTC)


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CloudEvidenceError(f"{label} must be UTC-aware")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
