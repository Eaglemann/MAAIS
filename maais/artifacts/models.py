from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final, cast
from uuid import UUID

from maais.config.artifacts import ArtifactType, RetentionMode
from maais.domain.json import MutableJsonValue
from maais.domain.json import content_hash as canonical_content_hash

DEFAULT_HASH_CHUNK_SIZE: Final = 1024 * 1024
GENESIS_EVIDENCE_HASH: Final = "0" * 64
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_KEY_SEGMENT_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")

ALLOWED_ARTIFACT_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/gzip",
        "application/json",
        "application/octet-stream",
        "application/vnd.apache.parquet",
        "application/x-ndjson",
        "application/zip",
        "application/zstd",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)


def validate_object_key(key: str) -> str:
    if not key or len(key) > 1024:
        raise ValueError("artifact object key must contain 1 to 1024 characters")
    if key.startswith("/") or key.endswith("/"):
        raise ValueError("artifact object key must be relative and must name an object")
    if "\\" in key:
        raise ValueError("artifact object key must use forward slashes")
    if "%" in key:
        raise ValueError("artifact object key must not use percent-encoded path syntax")
    if any(ord(character) < 32 or ord(character) == 127 for character in key):
        raise ValueError("artifact object key must not contain control characters")
    parts = key.split("/")
    if any(not part for part in parts):
        raise ValueError("artifact object key must not contain empty path components")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("artifact object key must not contain traversal components")
    if any(part.casefold() == "latest" for part in parts):
        raise ValueError("artifact object key must not contain mutable latest components")
    return key


def _validate_identity_segment(name: str, value: str) -> str:
    if _KEY_SEGMENT_PATTERN.fullmatch(value) is None or value.casefold() == "latest":
        raise ValueError(f"artifact {name} is not a canonical identity segment")
    return value


def artifact_key(
    *,
    environment: str,
    candidate_hash: str,
    experiment_id: UUID,
    artifact_type: str,
    report_id: str,
    relative_path: str,
) -> str:
    _validate_identity_segment("environment", environment)
    if _SHA256_PATTERN.fullmatch(candidate_hash) is None:
        raise ValueError("artifact candidate hash must be a lowercase SHA-256 digest")
    _validate_identity_segment("type", artifact_type)
    _validate_identity_segment("report ID", report_id)
    validate_object_key(relative_path)
    return validate_object_key(
        "/".join(
            (
                "maais",
                environment,
                candidate_hash,
                str(experiment_id),
                artifact_type,
                report_id,
                relative_path,
            )
        )
    )


def validate_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("artifact SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def validate_content_type(value: str) -> str:
    if value not in ALLOWED_ARTIFACT_CONTENT_TYPES:
        raise ValueError("artifact content type is not allowlisted")
    return value


def validate_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is not timezone.utc:
        raise ValueError(f"artifact {field_name} must use the UTC timezone")
    return value


@dataclass(frozen=True, slots=True)
class RetentionRequest:
    mode: RetentionMode
    retain_until: datetime

    def __post_init__(self) -> None:
        validate_utc(self.retain_until, field_name="retention timestamp")


@dataclass(frozen=True, slots=True)
class FileDigest:
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        validate_sha256(self.sha256)
        if self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")


@dataclass(frozen=True, slots=True)
class BundleFileExpectation:
    relative_path: str
    sha256: str
    size_bytes: int
    content_type: str

    def __post_init__(self) -> None:
        validate_object_key(self.relative_path)
        validate_sha256(self.sha256)
        if self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        validate_content_type(self.content_type)


@dataclass(frozen=True, slots=True)
class VerifiedBundleFile:
    path: Path
    relative_path: str
    sha256: str
    size_bytes: int
    content_type: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("verified artifact path must be absolute")
        validate_object_key(self.relative_path)
        validate_sha256(self.sha256)
        if self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        validate_content_type(self.content_type)


@dataclass(frozen=True, slots=True)
class ArtifactWriteRequest:
    key: str
    source_path: Path
    sha256: str
    size_bytes: int
    content_type: str
    retention: RetentionRequest

    def __post_init__(self) -> None:
        validate_object_key(self.key)
        if not self.source_path.is_absolute():
            raise ValueError("artifact source path must be absolute")
        validate_sha256(self.sha256)
        if self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        validate_content_type(self.content_type)

    @classmethod
    def from_verified_file(
        cls,
        *,
        key: str,
        source: VerifiedBundleFile,
        retention: RetentionRequest,
    ) -> ArtifactWriteRequest:
        return cls(
            key=key,
            source_path=source.path,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            content_type=source.content_type,
            retention=retention,
        )


class ArtifactPutDisposition(StrEnum):
    CREATED = "created"
    IDENTICAL_RETRY = "identical_retry"


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    store_name: str
    key: str
    etag: str
    version_id: str | None
    sha256: str
    size_bytes: int
    content_type: str
    retention: RetentionRequest
    stored_at: datetime

    def __post_init__(self) -> None:
        if not self.store_name.strip():
            raise ValueError("artifact store name must not be empty")
        validate_object_key(self.key)
        if not self.etag.strip():
            raise ValueError("artifact ETag must not be empty")
        if self.version_id is not None and not self.version_id.strip():
            raise ValueError("artifact version ID must not be empty")
        validate_sha256(self.sha256)
        if self.size_bytes < 0:
            raise ValueError("artifact size must be non-negative")
        validate_content_type(self.content_type)
        validate_utc(self.stored_at, field_name="stored timestamp")


@dataclass(frozen=True, slots=True)
class ArtifactPutResult:
    artifact: StoredArtifact
    disposition: ArtifactPutDisposition


@dataclass(frozen=True, slots=True)
class StoreCapabilities:
    store_name: str
    immutable_create: bool
    exact_version_reads: bool
    versioning_enabled: bool
    object_lock_enabled: bool
    compliance_retention_supported: bool

    def __post_init__(self) -> None:
        if not self.store_name.strip():
            raise ValueError("artifact store name must not be empty")


class ScheduledOperationType(StrEnum):
    DAILY_REPORT = "daily_report"
    LOGICAL_BACKUP = "logical_backup"
    AUDIT_EXPORT = "audit_export"
    ARTIFACT_PUBLICATION = "artifact_publication"
    QUALIFICATION = "qualification"
    RESTORE_DRILL = "restore_drill"
    PROCESS_DRILL = "process_drill"
    PREFLIGHT = "preflight"
    SOAK_VERDICT = "soak_verdict"
    FINAL_REPORT = "final_report"


class ScheduledOperationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PublicationAttemptStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ScheduledOperation:
    id: UUID
    run_id: UUID
    experiment_id: UUID
    operation_type: ScheduledOperationType
    berlin_date: date
    status: ScheduledOperationStatus
    owner_boot_id: UUID
    generated_at: datetime
    attempt: int
    result_artifact_ids: tuple[UUID, ...]
    reason_code: str | None
    started_at: datetime
    completed_at: datetime | None
    content_hash: str

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("run_id", self.run_id),
            ("experiment_id", self.experiment_id),
            ("owner_boot_id", self.owner_boot_id),
        ):
            _validate_uuid(name, value)
        if not isinstance(self.operation_type, ScheduledOperationType):
            raise ValueError("operation_type must be a ScheduledOperationType")
        if not isinstance(self.berlin_date, date) or isinstance(self.berlin_date, datetime):
            raise ValueError("berlin_date must be a date")
        if not isinstance(self.status, ScheduledOperationStatus):
            raise ValueError("status must be a ScheduledOperationStatus")
        _validate_utc_offset(self.generated_at, "generated_at")
        _validate_utc_offset(self.started_at, "started_at")
        if self.started_at < self.generated_at:
            raise ValueError("operation started_at cannot precede generated_at")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("operation attempt must be positive")
        for artifact_id in self.result_artifact_ids:
            _validate_uuid("result_artifact_id", artifact_id)
        if len(set(self.result_artifact_ids)) != len(self.result_artifact_ids):
            raise ValueError("operation result artifact IDs must be unique")
        if self.completed_at is not None:
            _validate_utc_offset(self.completed_at, "completed_at")
            if self.completed_at < self.started_at:
                raise ValueError("operation completed_at cannot precede started_at")
        if self.status is ScheduledOperationStatus.RUNNING:
            if self.completed_at is not None or self.reason_code is not None:
                raise ValueError("running operation cannot have terminal evidence")
        elif self.status is ScheduledOperationStatus.FAILED:
            if self.completed_at is None or self.reason_code is None:
                raise ValueError("failed operation requires terminal evidence")
            _validate_trimmed("reason_code", self.reason_code)
        elif (
            self.completed_at is None
            or self.reason_code is not None
            or not self.result_artifact_ids
        ):
            raise ValueError("succeeded operation lifecycle is invalid")
        _validate_sha256_field("content_hash", self.content_hash)
        if canonical_content_hash(self.content_payload()) != self.content_hash:
            raise ValueError("scheduled operation content hash mismatch")

    @classmethod
    def start(
        cls,
        *,
        operation_id: UUID,
        run_id: UUID,
        experiment_id: UUID,
        operation_type: ScheduledOperationType,
        berlin_date: date,
        owner_boot_id: UUID,
        generated_at: datetime,
        started_at: datetime,
    ) -> ScheduledOperation:
        return cls._build(
            id=operation_id,
            run_id=run_id,
            experiment_id=experiment_id,
            operation_type=operation_type,
            berlin_date=berlin_date,
            status=ScheduledOperationStatus.RUNNING,
            owner_boot_id=owner_boot_id,
            generated_at=generated_at,
            attempt=1,
            result_artifact_ids=(),
            reason_code=None,
            started_at=started_at,
            completed_at=None,
        )

    def takeover(self, *, owner_boot_id: UUID, started_at: datetime) -> ScheduledOperation:
        if self.status is ScheduledOperationStatus.SUCCEEDED:
            raise ValueError("succeeded operation cannot be taken over")
        return self._build(
            id=self.id,
            run_id=self.run_id,
            experiment_id=self.experiment_id,
            operation_type=self.operation_type,
            berlin_date=self.berlin_date,
            status=ScheduledOperationStatus.RUNNING,
            owner_boot_id=owner_boot_id,
            generated_at=self.generated_at,
            attempt=self.attempt + 1,
            result_artifact_ids=self.result_artifact_ids,
            reason_code=None,
            started_at=started_at,
            completed_at=None,
        )

    def fail(self, *, reason_code: str, failed_at: datetime) -> ScheduledOperation:
        if self.status is not ScheduledOperationStatus.RUNNING:
            raise ValueError("only a running operation can fail")
        return self._build(
            **self._identity_values(),
            status=ScheduledOperationStatus.FAILED,
            owner_boot_id=self.owner_boot_id,
            attempt=self.attempt,
            result_artifact_ids=self.result_artifact_ids,
            reason_code=reason_code,
            started_at=self.started_at,
            completed_at=failed_at,
        )

    def succeed(
        self,
        *,
        result_artifact_ids: tuple[UUID, ...],
        completed_at: datetime,
    ) -> ScheduledOperation:
        if self.status is not ScheduledOperationStatus.RUNNING:
            raise ValueError("only a running operation can succeed")
        return self._build(
            **self._identity_values(),
            status=ScheduledOperationStatus.SUCCEEDED,
            owner_boot_id=self.owner_boot_id,
            attempt=self.attempt,
            result_artifact_ids=result_artifact_ids,
            reason_code=None,
            started_at=self.started_at,
            completed_at=completed_at,
        )

    def content_payload(self) -> dict[str, MutableJsonValue]:
        return {
            "attempt": self.attempt,
            "berlin_date": self.berlin_date.isoformat(),
            "completed_at": _optional_datetime(self.completed_at),
            "experiment_id": str(self.experiment_id),
            "generated_at": _datetime_json(self.generated_at),
            "id": str(self.id),
            "operation_type": self.operation_type.value,
            "owner_boot_id": str(self.owner_boot_id),
            "reason_code": self.reason_code,
            "result_artifact_ids": [str(value) for value in self.result_artifact_ids],
            "run_id": str(self.run_id),
            "started_at": _datetime_json(self.started_at),
            "status": self.status.value,
        }

    def _identity_values(self) -> dict[str, object]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "operation_type": self.operation_type,
            "berlin_date": self.berlin_date,
            "generated_at": self.generated_at,
        }

    @classmethod
    def _build(cls, **values: object) -> ScheduledOperation:
        payload = _scheduled_payload(values)
        return cls(**values, content_hash=canonical_content_hash(payload))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ArtifactPublicationAttempt:
    id: UUID
    operation_id: UUID
    attempt: int
    bundle_content_hash: str
    status: PublicationAttemptStatus
    started_at: datetime
    completed_at: datetime | None
    reason_code: str | None
    content_hash: str

    def __post_init__(self) -> None:
        _validate_uuid("id", self.id)
        _validate_uuid("operation_id", self.operation_id)
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("publication attempt must be positive")
        _validate_sha256_field("bundle_content_hash", self.bundle_content_hash)
        if not isinstance(self.status, PublicationAttemptStatus):
            raise ValueError("status must be a PublicationAttemptStatus")
        _validate_utc_offset(self.started_at, "started_at")
        if self.completed_at is not None:
            _validate_utc_offset(self.completed_at, "completed_at")
            if self.completed_at < self.started_at:
                raise ValueError("attempt completed_at cannot precede started_at")
        if self.status is PublicationAttemptStatus.STARTED:
            if self.completed_at is not None or self.reason_code is not None:
                raise ValueError("started publication attempt cannot be terminal")
        elif self.status is PublicationAttemptStatus.FAILED:
            if self.completed_at is None or self.reason_code is None:
                raise ValueError("failed publication attempt requires a reason and time")
            _validate_trimmed("reason_code", self.reason_code)
        elif self.completed_at is None or self.reason_code is not None:
            raise ValueError("succeeded publication attempt lifecycle is invalid")
        _validate_sha256_field("content_hash", self.content_hash)
        if canonical_content_hash(self.content_payload()) != self.content_hash:
            raise ValueError("publication attempt content hash mismatch")

    @classmethod
    def start(
        cls,
        *,
        attempt_id: UUID,
        operation_id: UUID,
        attempt: int,
        bundle_content_hash: str,
        started_at: datetime,
    ) -> ArtifactPublicationAttempt:
        return cls._build(
            id=attempt_id,
            operation_id=operation_id,
            attempt=attempt,
            bundle_content_hash=bundle_content_hash,
            status=PublicationAttemptStatus.STARTED,
            started_at=started_at,
            completed_at=None,
            reason_code=None,
        )

    def fail(
        self,
        *,
        reason_code: str,
        failed_at: datetime,
    ) -> ArtifactPublicationAttempt:
        if self.status is not PublicationAttemptStatus.STARTED:
            raise ValueError("only a started publication attempt can fail")
        return self._build(
            id=self.id,
            operation_id=self.operation_id,
            attempt=self.attempt,
            bundle_content_hash=self.bundle_content_hash,
            status=PublicationAttemptStatus.FAILED,
            started_at=self.started_at,
            completed_at=failed_at,
            reason_code=reason_code,
        )

    def succeed(self, *, completed_at: datetime) -> ArtifactPublicationAttempt:
        if self.status is not PublicationAttemptStatus.STARTED:
            raise ValueError("only a started publication attempt can succeed")
        return self._build(
            id=self.id,
            operation_id=self.operation_id,
            attempt=self.attempt,
            bundle_content_hash=self.bundle_content_hash,
            status=PublicationAttemptStatus.SUCCEEDED,
            started_at=self.started_at,
            completed_at=completed_at,
            reason_code=None,
        )

    def content_payload(self) -> dict[str, MutableJsonValue]:
        return {
            "attempt": self.attempt,
            "bundle_content_hash": self.bundle_content_hash,
            "completed_at": _optional_datetime(self.completed_at),
            "id": str(self.id),
            "operation_id": str(self.operation_id),
            "reason_code": self.reason_code,
            "started_at": _datetime_json(self.started_at),
            "status": self.status.value,
        }

    @classmethod
    def _build(cls, **values: object) -> ArtifactPublicationAttempt:
        payload = _attempt_payload(values)
        return cls(**values, content_hash=canonical_content_hash(payload))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: UUID
    operation_id: UUID
    publication_attempt_id: UUID
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    artifact_type: ArtifactType
    report_id: str
    bundle_content_hash: str
    size_bytes: int
    media_type: str
    generated_at: datetime
    recorded_at: datetime
    producing_deployment_id: str
    producing_service_id: str
    sequence: int
    replica_inventory: tuple[StoredArtifact, ...]
    canonical_inventory: tuple[StoredArtifact, ...]
    previous_evidence_hash: str
    catalog_content_hash: str

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("operation_id", self.operation_id),
            ("publication_attempt_id", self.publication_attempt_id),
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
        ):
            _validate_uuid(name, value)
        if self.environment not in {"qualification", "production"}:
            raise ValueError("artifact environment must be qualification or production")
        _validate_sha256_field("candidate_hash", self.candidate_hash)
        if not isinstance(self.artifact_type, ArtifactType):
            raise ValueError("artifact_type must be an ArtifactType")
        _validate_identity_segment("report_id", self.report_id)
        _validate_sha256_field("bundle_content_hash", self.bundle_content_hash)
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("artifact record size must be non-negative")
        validate_content_type(self.media_type)
        _validate_utc_offset(self.generated_at, "generated_at")
        _validate_utc_offset(self.recorded_at, "recorded_at")
        if self.recorded_at < self.generated_at:
            raise ValueError("artifact recorded_at cannot precede generated_at")
        _validate_trimmed("producing_deployment_id", self.producing_deployment_id)
        _validate_trimmed("producing_service_id", self.producing_service_id)
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("artifact catalog sequence must be positive")
        if not self.replica_inventory or not self.canonical_inventory:
            raise ValueError("artifact record requires both target inventories")
        if len(self.replica_inventory) != len(self.canonical_inventory):
            raise ValueError("artifact target inventories must have equal cardinality")
        for replica, canonical in zip(
            self.replica_inventory,
            self.canonical_inventory,
            strict=True,
        ):
            if canonical.version_id is None:
                raise ValueError("canonical artifact inventory requires provider version IDs")
            if replica.store_name == canonical.store_name:
                raise ValueError("replica and canonical inventory stores must be distinct")
            if _stored_content_identity(replica) != _stored_content_identity(canonical):
                raise ValueError("replica and canonical inventory content differs")
            if not (
                self.generated_at <= replica.stored_at <= self.recorded_at
                and self.generated_at <= canonical.stored_at <= self.recorded_at
            ):
                raise ValueError("artifact inventory timestamps are outside publication bounds")
            if canonical.retention.retain_until <= self.recorded_at:
                raise ValueError("canonical artifact retention must extend beyond publication")
        if sum(item.size_bytes for item in self.replica_inventory) != self.size_bytes:
            raise ValueError("artifact record size does not equal its inventory")
        _validate_sha256_field("previous_evidence_hash", self.previous_evidence_hash)
        _validate_sha256_field("catalog_content_hash", self.catalog_content_hash)
        if canonical_content_hash(self.content_payload()) != self.catalog_content_hash:
            raise ValueError("artifact catalog content hash mismatch")

    @classmethod
    def create(
        cls,
        *,
        record_id: UUID,
        operation_id: UUID,
        publication_attempt_id: UUID,
        environment: str,
        candidate_hash: str,
        experiment_id: UUID,
        run_id: UUID,
        artifact_type: ArtifactType,
        report_id: str,
        bundle_content_hash: str,
        size_bytes: int,
        media_type: str,
        generated_at: datetime,
        recorded_at: datetime,
        producing_deployment_id: str,
        producing_service_id: str,
        sequence: int,
        replica_inventory: tuple[StoredArtifact, ...],
        canonical_inventory: tuple[StoredArtifact, ...],
        previous_evidence_hash: str,
    ) -> ArtifactRecord:
        values: dict[str, object] = {
            "id": record_id,
            "operation_id": operation_id,
            "publication_attempt_id": publication_attempt_id,
            "environment": environment,
            "candidate_hash": candidate_hash,
            "experiment_id": experiment_id,
            "run_id": run_id,
            "artifact_type": artifact_type,
            "report_id": report_id,
            "bundle_content_hash": bundle_content_hash,
            "size_bytes": size_bytes,
            "media_type": media_type,
            "generated_at": generated_at,
            "recorded_at": recorded_at,
            "producing_deployment_id": producing_deployment_id,
            "producing_service_id": producing_service_id,
            "sequence": sequence,
            "replica_inventory": replica_inventory,
            "canonical_inventory": canonical_inventory,
            "previous_evidence_hash": previous_evidence_hash,
        }
        payload = _record_payload(values)
        return cls(
            id=record_id,
            operation_id=operation_id,
            publication_attempt_id=publication_attempt_id,
            environment=environment,
            candidate_hash=candidate_hash,
            experiment_id=experiment_id,
            run_id=run_id,
            artifact_type=artifact_type,
            report_id=report_id,
            bundle_content_hash=bundle_content_hash,
            size_bytes=size_bytes,
            media_type=media_type,
            generated_at=generated_at,
            recorded_at=recorded_at,
            producing_deployment_id=producing_deployment_id,
            producing_service_id=producing_service_id,
            sequence=sequence,
            replica_inventory=replica_inventory,
            canonical_inventory=canonical_inventory,
            previous_evidence_hash=previous_evidence_hash,
            catalog_content_hash=canonical_content_hash(payload),
        )

    def content_payload(self) -> dict[str, MutableJsonValue]:
        return _record_payload(
            {
                "id": self.id,
                "operation_id": self.operation_id,
                "publication_attempt_id": self.publication_attempt_id,
                "environment": self.environment,
                "candidate_hash": self.candidate_hash,
                "experiment_id": self.experiment_id,
                "run_id": self.run_id,
                "artifact_type": self.artifact_type,
                "report_id": self.report_id,
                "bundle_content_hash": self.bundle_content_hash,
                "size_bytes": self.size_bytes,
                "media_type": self.media_type,
                "generated_at": self.generated_at,
                "recorded_at": self.recorded_at,
                "producing_deployment_id": self.producing_deployment_id,
                "producing_service_id": self.producing_service_id,
                "sequence": self.sequence,
                "replica_inventory": self.replica_inventory,
                "canonical_inventory": self.canonical_inventory,
                "previous_evidence_hash": self.previous_evidence_hash,
            }
        )


def stored_artifact_json(value: StoredArtifact) -> dict[str, MutableJsonValue]:
    return {
        "content_type": value.content_type,
        "etag": value.etag,
        "key": value.key,
        "retention_mode": value.retention.mode.value,
        "retain_until": _datetime_json(value.retention.retain_until),
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
        "store_name": value.store_name,
        "stored_at": _datetime_json(value.stored_at),
        "version_id": value.version_id,
    }


def stored_artifact_from_json(value: object) -> StoredArtifact:
    if not isinstance(value, dict):
        raise ValueError("stored artifact inventory item must be an object")
    try:
        return StoredArtifact(
            store_name=str(value["store_name"]),
            key=str(value["key"]),
            etag=str(value["etag"]),
            version_id=(str(value["version_id"]) if value["version_id"] is not None else None),
            sha256=str(value["sha256"]),
            size_bytes=int(value["size_bytes"]),
            content_type=str(value["content_type"]),
            retention=RetentionRequest(
                mode=RetentionMode(str(value["retention_mode"])),
                retain_until=_parse_catalog_datetime(str(value["retain_until"])),
            ),
            stored_at=_parse_catalog_datetime(str(value["stored_at"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("stored artifact inventory item is invalid") from error


def _scheduled_payload(values: dict[str, object]) -> dict[str, MutableJsonValue]:
    return {
        "attempt": cast(int, values["attempt"]),
        "berlin_date": cast(date, values["berlin_date"]).isoformat(),
        "completed_at": _optional_datetime(cast(datetime | None, values["completed_at"])),
        "experiment_id": str(values["experiment_id"]),
        "generated_at": _datetime_json(values["generated_at"]),
        "id": str(values["id"]),
        "operation_type": str(values["operation_type"]),
        "owner_boot_id": str(values["owner_boot_id"]),
        "reason_code": cast(str | None, values["reason_code"]),
        "result_artifact_ids": [
            str(value) for value in cast(tuple[UUID, ...], values["result_artifact_ids"])
        ],
        "run_id": str(values["run_id"]),
        "started_at": _datetime_json(values["started_at"]),
        "status": str(values["status"]),
    }


def _attempt_payload(values: dict[str, object]) -> dict[str, MutableJsonValue]:
    return {
        "attempt": cast(int, values["attempt"]),
        "bundle_content_hash": str(values["bundle_content_hash"]),
        "completed_at": _optional_datetime(cast(datetime | None, values["completed_at"])),
        "id": str(values["id"]),
        "operation_id": str(values["operation_id"]),
        "reason_code": cast(str | None, values["reason_code"]),
        "started_at": _datetime_json(values["started_at"]),
        "status": str(values["status"]),
    }


def _record_payload(values: dict[str, object]) -> dict[str, MutableJsonValue]:
    return {
        "artifact_type": str(values["artifact_type"]),
        "bundle_content_hash": str(values["bundle_content_hash"]),
        "candidate_hash": str(values["candidate_hash"]),
        "canonical_inventory": [
            stored_artifact_json(value)
            for value in cast(
                tuple[StoredArtifact, ...],
                values["canonical_inventory"],
            )
        ],
        "environment": str(values["environment"]),
        "experiment_id": str(values["experiment_id"]),
        "generated_at": _datetime_json(values["generated_at"]),
        "id": str(values["id"]),
        "media_type": str(values["media_type"]),
        "operation_id": str(values["operation_id"]),
        "previous_evidence_hash": str(values["previous_evidence_hash"]),
        "producing_deployment_id": str(values["producing_deployment_id"]),
        "producing_service_id": str(values["producing_service_id"]),
        "publication_attempt_id": str(values["publication_attempt_id"]),
        "recorded_at": _datetime_json(values["recorded_at"]),
        "replica_inventory": [
            stored_artifact_json(value)
            for value in cast(
                tuple[StoredArtifact, ...],
                values["replica_inventory"],
            )
        ],
        "report_id": str(values["report_id"]),
        "run_id": str(values["run_id"]),
        "sequence": cast(int, values["sequence"]),
        "size_bytes": cast(int, values["size_bytes"]),
    }


def _stored_content_identity(value: StoredArtifact) -> tuple[object, ...]:
    return (
        value.key,
        value.sha256,
        value.size_bytes,
        value.content_type,
        value.retention,
    )


def _validate_uuid(name: str, value: object) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{name} must be a non-nil UUID")


def _validate_sha256_field(name: str, value: object) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _validate_trimmed(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty and trimmed")


def _validate_utc_offset(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC-aware")


def _datetime_json(value: object) -> str:
    if not isinstance(value, datetime):
        raise ValueError("catalog datetime value is invalid")
    _validate_utc_offset(value, "datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_datetime(value: datetime | None) -> str | None:
    return _datetime_json(value) if value is not None else None


def _parse_catalog_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _validate_utc_offset(parsed, "datetime")
    return parsed.astimezone(timezone.utc)
