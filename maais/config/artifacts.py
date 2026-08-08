from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class ArtifactStoreMode(StrEnum):
    FILESYSTEM = "filesystem"
    DUAL_S3 = "dual_s3"


class RetentionMode(StrEnum):
    GOVERNANCE = "GOVERNANCE"
    COMPLIANCE = "COMPLIANCE"


class ArtifactType(StrEnum):
    QUALIFICATION_WORKING = "qualification_working"
    DAILY_REPORT = "daily_report"
    AUDIT_EXPORT = "audit_export"
    LOGICAL_BACKUP = "logical_backup"
    MANIFEST = "manifest"
    QUALIFICATION_EVIDENCE = "qualification_evidence"
    RESTORE_DRILL = "restore_drill"
    PROCESS_DRILL = "process_drill"
    PREFLIGHT = "preflight"
    SOAK_VERDICT = "soak_verdict"
    FINAL_REPORT = "final_report"


class RetentionSettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    qualification_days: Literal[30] = 30
    operational_days: Literal[90] = 90
    official_evidence_days: Literal[365] = 365


ARTIFACT_RETENTION_POLICIES: Mapping[ArtifactType, tuple[RetentionMode, int]] = MappingProxyType(
    {
        ArtifactType.QUALIFICATION_WORKING: (RetentionMode.GOVERNANCE, 30),
        ArtifactType.DAILY_REPORT: (RetentionMode.COMPLIANCE, 90),
        ArtifactType.AUDIT_EXPORT: (RetentionMode.COMPLIANCE, 90),
        ArtifactType.LOGICAL_BACKUP: (RetentionMode.COMPLIANCE, 90),
        ArtifactType.MANIFEST: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.QUALIFICATION_EVIDENCE: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.RESTORE_DRILL: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.PROCESS_DRILL: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.PREFLIGHT: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.SOAK_VERDICT: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.FINAL_REPORT: (RetentionMode.COMPLIANCE, 365),
    }
)


class ArtifactSettings(BaseModel):
    """Secret-safe configuration for local or independently replicated evidence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    mode: ArtifactStoreMode = ArtifactStoreMode.FILESYSTEM
    local_root: Path = Path("artifacts")

    replica_endpoint_url: str = ""
    replica_region: str = ""
    replica_bucket: str = ""
    replica_access_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    replica_secret_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    replica_session_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )

    canonical_endpoint_url: str = ""
    canonical_region: str = ""
    canonical_bucket: str = ""
    canonical_access_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    canonical_secret_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    canonical_session_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )

    canonical_object_lock_required: Literal[True] = True
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    @field_validator(
        "replica_endpoint_url",
        "replica_region",
        "replica_bucket",
        "canonical_endpoint_url",
        "canonical_region",
        "canonical_bucket",
    )
    @classmethod
    def validate_trimmed_cloud_value(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("artifact target values must be trimmed")
        return value

    @field_validator("replica_endpoint_url", "canonical_endpoint_url")
    @classmethod
    def validate_endpoint_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact endpoint must be an HTTPS origin without credentials")
        normalized_path = parsed.path.rstrip("/")
        if normalized_path:
            raise ValueError("artifact endpoint must not contain a path")
        return value.rstrip("/")

    @field_validator("local_root")
    @classmethod
    def validate_local_root(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("artifact local root must be a normalized relative path")
        return value

    @model_validator(mode="after")
    def validate_storage_topology(self) -> Self:
        configured_values = self._target_values()
        any_configured = any(configured_values)
        all_configured = all(configured_values)
        if self.mode is ArtifactStoreMode.FILESYSTEM:
            if any_configured:
                raise ValueError("filesystem artifact mode forbids inactive cloud target settings")
            return self
        if not all_configured:
            raise ValueError("dual_s3 requires complete replica and canonical targets")
        if (
            self.replica_endpoint_url == self.canonical_endpoint_url
            or self.replica_bucket == self.canonical_bucket
            or self.replica_access_key_value == self.canonical_access_key_value
            or self.replica_secret_key_value == self.canonical_secret_key_value
        ):
            raise ValueError(
                "dual_s3 requires independent artifact targets with distinct providers, "
                "buckets, and credentials"
            )
        return self

    def _target_values(self) -> tuple[str, ...]:
        return (
            self.replica_endpoint_url,
            self.replica_region,
            self.replica_bucket,
            self.replica_access_key_value,
            self.replica_secret_key_value,
            self.canonical_endpoint_url,
            self.canonical_region,
            self.canonical_bucket,
            self.canonical_access_key_value,
            self.canonical_secret_key_value,
        )

    @property
    def replica_access_key_value(self) -> str:
        return self.replica_access_key.get_secret_value()

    @property
    def replica_secret_key_value(self) -> str:
        return self.replica_secret_key.get_secret_value()

    @property
    def replica_session_token_value(self) -> str:
        return self.replica_session_token.get_secret_value()

    @property
    def canonical_access_key_value(self) -> str:
        return self.canonical_access_key.get_secret_value()

    @property
    def canonical_secret_key_value(self) -> str:
        return self.canonical_secret_key.get_secret_value()

    @property
    def canonical_session_token_value(self) -> str:
        return self.canonical_session_token.get_secret_value()

    @property
    def replica_configured(self) -> bool:
        return all(self._target_values()[:5])

    @property
    def canonical_configured(self) -> bool:
        return all(self._target_values()[5:])

    def redacted_summary(self) -> dict[str, str | bool | int]:
        return {
            "mode": self.mode.value,
            "replica_configured": self.replica_configured,
            "canonical_configured": self.canonical_configured,
            "canonical_object_lock_required": self.canonical_object_lock_required,
            "qualification_retention_days": self.retention.qualification_days,
            "operational_retention_days": self.retention.operational_days,
            "official_evidence_retention_days": self.retention.official_evidence_days,
        }
