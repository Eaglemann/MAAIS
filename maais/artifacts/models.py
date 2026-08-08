from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

from maais.config.artifacts import RetentionMode

DEFAULT_HASH_CHUNK_SIZE: Final = 1024 * 1024
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
    version_id: str
    sha256: str
    size_bytes: int
    content_type: str
    retention: RetentionRequest
    stored_at: datetime

    def __post_init__(self) -> None:
        if not self.store_name.strip():
            raise ValueError("artifact store name must not be empty")
        validate_object_key(self.key)
        if not self.version_id.strip():
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
