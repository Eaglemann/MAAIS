import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from maais.config.artifacts import ArtifactSettings, ArtifactStoreMode
from maais.config.cloud import (
    DATABASE_ROLE_BY_SERVICE,
    EU_WEST_RAILWAY_REGION,
    CloudSettings,
    DeploymentTarget,
    ServiceRole,
)
from maais.config.modes import RunMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    run_mode: RunMode = RunMode.REPLAY
    binance_demo_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    binance_demo_api_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    database_url: SecretStr = Field(
        default_factory=lambda: SecretStr(
            "postgresql+psycopg://maais:maais@"  # pragma: allowlist secret
            "localhost:5432/maais"
        ),
        exclude=True,
        repr=False,
    )
    maais_test_database_url: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    duckdb_path: str = "./data/maais.duckdb"
    kafka_bootstrap_servers: str = "localhost:9092"
    log_level: str = "INFO"
    environment: str = "development"
    telegram_bot_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    telegram_chat_id: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    mission_control_token_file: Path | None = Path("artifacts/run-state/mission-control.token")
    deployment_target: DeploymentTarget = Field(
        default=DeploymentTarget.LOCAL,
        validation_alias="MAAIS_DEPLOYMENT_TARGET",
    )
    service_role: ServiceRole | None = Field(
        default=None,
        validation_alias="MAAIS_SERVICE_ROLE",
    )
    railway_project_id: str = ""
    railway_environment_id: str = ""
    railway_service_id: str = ""
    railway_deployment_id: str = ""
    railway_snapshot_id: str | None = None
    railway_replica_id: str = ""
    railway_region: str = Field(
        default="",
        validation_alias="RAILWAY_REPLICA_REGION",
    )
    expected_railway_region: str = Field(
        default="",
        validation_alias="MAAIS_EXPECTED_RAILWAY_REGION",
    )
    railway_git_commit_sha: str = Field(
        default="",
        validation_alias="RAILWAY_GIT_COMMIT_SHA",
    )
    candidate_descriptor_path: Path = Field(
        default=Path("/app/candidate.json"),
        validation_alias="MAAIS_CANDIDATE_DESCRIPTOR_PATH",
    )
    expected_schema_revision: str = Field(
        default="",
        validation_alias="MAAIS_EXPECTED_SCHEMA_REVISION",
    )
    database_role_name: str = Field(
        default="",
        validation_alias="MAAIS_DATABASE_ROLE_NAME",
    )
    artifact_store_mode: ArtifactStoreMode = Field(
        default=ArtifactStoreMode.FILESYSTEM,
        validation_alias="MAAIS_ARTIFACT_STORE_MODE",
    )
    artifact_local_root: Path = Field(
        default=Path("artifacts"),
        validation_alias="MAAIS_ARTIFACT_LOCAL_ROOT",
    )
    artifact_replica_endpoint_url: str = Field(
        default="",
        validation_alias="MAAIS_ARTIFACT_REPLICA_ENDPOINT_URL",
    )
    artifact_replica_region: str = Field(
        default="",
        validation_alias="MAAIS_ARTIFACT_REPLICA_REGION",
    )
    artifact_replica_bucket: str = Field(
        default="",
        validation_alias="MAAIS_ARTIFACT_REPLICA_BUCKET",
    )
    artifact_replica_access_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_ARTIFACT_REPLICA_ACCESS_KEY",
        exclude=True,
        repr=False,
    )
    artifact_replica_secret_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_ARTIFACT_REPLICA_SECRET_KEY",
        exclude=True,
        repr=False,
    )
    artifact_replica_session_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_ARTIFACT_REPLICA_SESSION_TOKEN",
        exclude=True,
        repr=False,
    )
    artifact_canonical_endpoint_url: str = Field(
        default="",
        validation_alias="MAAIS_ARTIFACT_CANONICAL_ENDPOINT_URL",
    )
    artifact_canonical_region: str = Field(
        default="",
        validation_alias="MAAIS_ARTIFACT_CANONICAL_REGION",
    )
    artifact_canonical_bucket: str = Field(
        default="",
        validation_alias="MAAIS_ARTIFACT_CANONICAL_BUCKET",
    )
    artifact_canonical_access_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_ARTIFACT_CANONICAL_ACCESS_KEY",
        exclude=True,
        repr=False,
    )
    artifact_canonical_secret_key: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_ARTIFACT_CANONICAL_SECRET_KEY",
        exclude=True,
        repr=False,
    )
    artifact_canonical_session_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_ARTIFACT_CANONICAL_SESSION_TOKEN",
        exclude=True,
        repr=False,
    )
    artifact_canonical_object_lock_required: Literal[True] = Field(
        default=True,
        validation_alias="MAAIS_ARTIFACT_CANONICAL_OBJECT_LOCK_REQUIRED",
    )

    @model_validator(mode="after")
    def validate_railway_identity(self) -> Self:
        if self.deployment_target is DeploymentTarget.LOCAL:
            _ = self.artifacts
            return self
        required = {
            "RAILWAY_PROJECT_ID": self.railway_project_id,
            "RAILWAY_ENVIRONMENT_ID": self.railway_environment_id,
            "RAILWAY_SERVICE_ID": self.railway_service_id,
            "RAILWAY_DEPLOYMENT_ID": self.railway_deployment_id,
            "RAILWAY_REPLICA_ID": self.railway_replica_id,
            "RAILWAY_REPLICA_REGION": self.railway_region,
            "MAAIS_EXPECTED_RAILWAY_REGION": self.expected_railway_region,
            "RAILWAY_GIT_COMMIT_SHA": self.railway_git_commit_sha,
            "MAAIS_EXPECTED_SCHEMA_REVISION": self.expected_schema_revision,
            "MAAIS_DATABASE_ROLE_NAME": self.database_role_name,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if self.service_role is None:
            missing.insert(0, "MAAIS_SERVICE_ROLE")
        if missing:
            raise ValueError(
                "Railway runtime requires non-empty identity fields: " + ", ".join(missing)
            )
        untrimmed = [name for name, value in required.items() if value != value.strip()]
        if self.railway_snapshot_id is not None and (
            self.railway_snapshot_id != self.railway_snapshot_id.strip()
        ):
            untrimmed.append("RAILWAY_SNAPSHOT_ID")
        if untrimmed:
            raise ValueError("Railway identity fields must be trimmed: " + ", ".join(untrimmed))
        if re.fullmatch(r"\d{4}", self.expected_schema_revision) is None:
            raise ValueError("Railway expected schema revision must be four decimal digits")
        if re.fullmatch(r"[0-9a-f]{40}", self.railway_git_commit_sha) is None:
            raise ValueError("Railway Git commit SHA must be 40 lowercase hexadecimal characters")
        if self.expected_railway_region != EU_WEST_RAILWAY_REGION:
            raise ValueError(
                "expected Railway region must be the frozen EU West region: "
                f"{EU_WEST_RAILWAY_REGION}"
            )
        if self.railway_region != self.expected_railway_region:
            raise ValueError(
                "unexpected Railway replica region: "
                f"expected={self.expected_railway_region} actual={self.railway_region}"
            )
        if (
            not self.candidate_descriptor_path.is_absolute()
            or ".." in self.candidate_descriptor_path.parts
        ):
            raise ValueError("Railway candidate descriptor path must be absolute and normalized")
        if self.run_mode is not RunMode.PAPER_LIVE:
            raise ValueError("Railway application services require run_mode=paper_live")
        if self.environment not in {"qualification", "production"}:
            raise ValueError("Railway deployment environment must be qualification or production")
        if self.run_mode is RunMode.PAPER_LIVE and (
            self.binance_demo_api_key_value or self.binance_demo_api_secret_value
        ):
            raise ValueError("Railway paper runtime forbids all authenticated exchange credentials")
        assert self.service_role is not None
        expected_role = DATABASE_ROLE_BY_SERVICE[self.service_role]
        if self.database_role_name != expected_role:
            raise ValueError(
                "Railway service database role mismatch: "
                f"service_role={self.service_role.value} expected={expected_role}"
            )
        if self.artifacts.mode is not ArtifactStoreMode.DUAL_S3:
            raise ValueError("Railway runtime requires MAAIS_ARTIFACT_STORE_MODE=dual_s3")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"qualification", "production"}

    @property
    def binance_demo_api_key_value(self) -> str:
        return self.binance_demo_api_key.get_secret_value()

    @property
    def binance_demo_api_secret_value(self) -> str:
        return self.binance_demo_api_secret.get_secret_value()

    @property
    def database_url_value(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def maais_test_database_url_value(self) -> str:
        return self.maais_test_database_url.get_secret_value()

    @property
    def telegram_bot_token_value(self) -> str:
        return self.telegram_bot_token.get_secret_value()

    @property
    def telegram_chat_id_value(self) -> str:
        return self.telegram_chat_id.get_secret_value()

    @property
    def cloud(self) -> CloudSettings:
        return CloudSettings(
            deployment_target=self.deployment_target,
            service_role=self.service_role,
            railway_project_id=self.railway_project_id,
            railway_environment_id=self.railway_environment_id,
            railway_service_id=self.railway_service_id,
            railway_deployment_id=self.railway_deployment_id,
            railway_snapshot_id=self.railway_snapshot_id,
            railway_replica_id=self.railway_replica_id,
            railway_region=self.railway_region,
            expected_railway_region=self.expected_railway_region,
            railway_git_commit_sha=self.railway_git_commit_sha,
            candidate_descriptor_path=self.candidate_descriptor_path,
            expected_schema_revision=self.expected_schema_revision,
            database_role_name=self.database_role_name,
        )

    @property
    def artifacts(self) -> ArtifactSettings:
        return ArtifactSettings(
            mode=self.artifact_store_mode,
            local_root=self.artifact_local_root,
            replica_endpoint_url=self.artifact_replica_endpoint_url,
            replica_region=self.artifact_replica_region,
            replica_bucket=self.artifact_replica_bucket,
            replica_access_key=self.artifact_replica_access_key,
            replica_secret_key=self.artifact_replica_secret_key,
            replica_session_token=self.artifact_replica_session_token,
            canonical_endpoint_url=self.artifact_canonical_endpoint_url,
            canonical_region=self.artifact_canonical_region,
            canonical_bucket=self.artifact_canonical_bucket,
            canonical_access_key=self.artifact_canonical_access_key,
            canonical_secret_key=self.artifact_canonical_secret_key,
            canonical_session_token=self.artifact_canonical_session_token,
            canonical_object_lock_required=self.artifact_canonical_object_lock_required,
        )

    def redacted_summary(self) -> dict[str, str | bool | int | None]:
        """Return the only settings fields approved for diagnostics and evidence."""
        return {
            "run_mode": self.run_mode.value,
            "environment": self.environment,
            "deployment_target": self.deployment_target.value,
            "service_role": self.service_role.value if self.service_role is not None else None,
            "is_production": self.is_production,
            "exchange_credentials_configured": bool(
                self.binance_demo_api_key_value or self.binance_demo_api_secret_value
            ),
            "railway_project_id": self.railway_project_id,
            "railway_environment_id": self.railway_environment_id,
            "railway_service_id": self.railway_service_id,
            "railway_deployment_id": self.railway_deployment_id,
            "railway_snapshot_id": self.railway_snapshot_id,
            "railway_replica_id": self.railway_replica_id,
            "railway_region": self.railway_region,
            "expected_railway_region": self.expected_railway_region,
            "railway_git_commit_sha": self.railway_git_commit_sha,
            "expected_schema_revision": self.expected_schema_revision,
            "database_role_name": self.database_role_name,
            "artifact_store_mode": self.artifact_store_mode.value,
            "artifact_replica_configured": self.artifacts.replica_configured,
            "artifact_canonical_configured": self.artifacts.canonical_configured,
            "artifact_canonical_object_lock_required": (
                self.artifacts.canonical_object_lock_required
            ),
            "artifact_qualification_retention_days": (self.artifacts.retention.qualification_days),
            "artifact_operational_retention_days": self.artifacts.retention.operational_days,
            "artifact_official_evidence_retention_days": (
                self.artifacts.retention.official_evidence_days
            ),
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
