import re
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

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
from maais.config.observability import LogFormat, ObservabilitySettings
from maais.config.security import AuthMode, SecuritySettings


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
    restore_target_database_url: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_RESTORE_TARGET_DATABASE_URL",
        exclude=True,
        repr=False,
    )
    duckdb_path: str = "./data/maais.duckdb"
    kafka_bootstrap_servers: str = "localhost:9092"
    log_level: str = "INFO"
    environment: str = "development"
    log_format: LogFormat = Field(
        default=LogFormat.CONSOLE,
        validation_alias="MAAIS_LOG_FORMAT",
    )
    sentry_backend_dsn: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="SENTRY_DSN",
        exclude=True,
        repr=False,
    )
    sentry_browser_dsn: str = Field(
        default="",
        validation_alias="VITE_SENTRY_DSN",
    )
    sentry_traces_sample_rate: float = Field(
        default=0.0,
        validation_alias="SENTRY_TRACES_SAMPLE_RATE",
    )
    sentry_profiles_sample_rate: float = Field(
        default=0.0,
        validation_alias="SENTRY_PROFILES_SAMPLE_RATE",
    )
    sentry_send_default_pii: Literal[False] = Field(
        default=False,
        validation_alias="SENTRY_SEND_DEFAULT_PII",
    )
    sentry_session_replay_enabled: Literal[False] = Field(
        default=False,
        validation_alias="MAAIS_SENTRY_SESSION_REPLAY_ENABLED",
    )
    sentry_daily_close_monitor_slug: str = Field(
        default="",
        validation_alias="MAAIS_SENTRY_DAILY_CLOSE_MONITOR_SLUG",
    )
    sentry_backup_monitor_slug: str = Field(
        default="",
        validation_alias="MAAIS_SENTRY_BACKUP_MONITOR_SLUG",
    )
    sentry_evidence_monitor_slug: str = Field(
        default="",
        validation_alias="MAAIS_SENTRY_EVIDENCE_MONITOR_SLUG",
    )
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
    cloud_run_id: UUID | None = Field(
        default=None,
        validation_alias="MAAIS_RUN_ID",
    )
    manifest_artifact_id: UUID | None = Field(
        default=None,
        validation_alias="MAAIS_MANIFEST_ARTIFACT_ID",
    )
    port: int = Field(
        default=8000,
        validation_alias="PORT",
        ge=1,
        le=65_535,
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
    auth_mode: AuthMode = Field(
        default=AuthMode.LOCAL_TOKEN,
        validation_alias="MAAIS_AUTH_MODE",
    )
    operator_password_hash: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_OPERATOR_PASSWORD_HASH",
        exclude=True,
        repr=False,
    )
    session_pepper: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_SESSION_PEPPER",
        exclude=True,
        repr=False,
    )
    csrf_pepper: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_CSRF_PEPPER",
        exclude=True,
        repr=False,
    )
    monitor_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        validation_alias="MAAIS_MONITOR_TOKEN",
        exclude=True,
        repr=False,
    )
    operator_secure_cookies: bool = Field(
        default=True,
        validation_alias="MAAIS_OPERATOR_SECURE_COOKIES",
    )
    operator_public_origin: str = Field(
        default="",
        validation_alias="MAAIS_OPERATOR_PUBLIC_ORIGIN",
    )

    @model_validator(mode="after")
    def validate_railway_identity(self) -> Self:
        if self.deployment_target is DeploymentTarget.LOCAL:
            _ = self.artifacts
            _ = self.security
            _ = self.observability
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
        if self.cloud_run_id is not None and self.cloud_run_id.int == 0:
            raise ValueError("Railway run identifier must be a non-nil UUID")
        if self.manifest_artifact_id is not None and self.manifest_artifact_id.int == 0:
            raise ValueError("Railway manifest artifact identifier must be a non-nil UUID")
        assert self.service_role is not None
        if self.service_role is ServiceRole.WORKER:
            if self.cloud_run_id is None or self.manifest_artifact_id is None:
                raise ValueError(
                    "Railway worker requires MAAIS_RUN_ID and MAAIS_MANIFEST_ARTIFACT_ID"
                )
        elif self.service_role in {ServiceRole.OPERATIONS, ServiceRole.VERIFIER}:
            if self.cloud_run_id is None:
                raise ValueError("Railway operations and verifier roles require MAAIS_RUN_ID")
            if self.manifest_artifact_id is not None:
                raise ValueError("only the worker role may receive MAAIS_MANIFEST_ARTIFACT_ID")
        elif self.cloud_run_id is not None or self.manifest_artifact_id is not None:
            raise ValueError("Railway web and migrator roles forbid run identity variables")
        expected_role = DATABASE_ROLE_BY_SERVICE[self.service_role]
        if self.database_role_name != expected_role:
            raise ValueError(
                "Railway service database role mismatch: "
                f"service_role={self.service_role.value} expected={expected_role}"
            )
        artifacts = self.artifacts
        if self.service_role is ServiceRole.WORKER:
            if artifacts.mode is not ArtifactStoreMode.CANONICAL_READ:
                raise ValueError("Railway worker requires MAAIS_ARTIFACT_STORE_MODE=canonical_read")
        elif self.service_role is ServiceRole.OPERATIONS:
            if artifacts.mode is not ArtifactStoreMode.DUAL_S3:
                raise ValueError("Railway operations requires MAAIS_ARTIFACT_STORE_MODE=dual_s3")
        elif artifacts.mode is not ArtifactStoreMode.FILESYSTEM:
            raise ValueError("Railway service role does not have artifact authority")
        _ = self.security
        _ = self.observability
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
    def restore_target_database_url_value(self) -> str:
        return self.restore_target_database_url.get_secret_value()

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

    @property
    def security(self) -> SecuritySettings:
        return SecuritySettings(
            deployment_target=self.deployment_target,
            service_role=self.service_role,
            auth_mode=self.auth_mode,
            operator_password_hash=self.operator_password_hash,
            session_pepper=self.session_pepper,
            csrf_pepper=self.csrf_pepper,
            monitor_token=self.monitor_token,
            secure_cookies=self.operator_secure_cookies,
            public_origin=self.operator_public_origin,
        )

    @property
    def observability(self) -> ObservabilitySettings:
        return ObservabilitySettings(
            deployment_target=self.deployment_target,
            service_role=self.service_role,
            environment=self.environment,
            release=self.railway_git_commit_sha,
            log_format=self.log_format,
            backend_dsn=self.sentry_backend_dsn,
            browser_dsn=self.sentry_browser_dsn,
            traces_sample_rate=self.sentry_traces_sample_rate,
            profiles_sample_rate=self.sentry_profiles_sample_rate,
            send_default_pii=self.sentry_send_default_pii,
            session_replay_enabled=self.sentry_session_replay_enabled,
            daily_close_monitor_slug=self.sentry_daily_close_monitor_slug,
            backup_monitor_slug=self.sentry_backup_monitor_slug,
            evidence_monitor_slug=self.sentry_evidence_monitor_slug,
        )

    def redacted_summary(self) -> dict[str, str | bool | int | float | None]:
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
            "cloud_run_id": str(self.cloud_run_id) if self.cloud_run_id is not None else None,
            "manifest_artifact_id": (
                str(self.manifest_artifact_id) if self.manifest_artifact_id is not None else None
            ),
            "port": self.port,
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
            "auth_mode": self.security.auth_mode.value,
            "operator_session_configured": self.security.operator_session_configured,
            "operator_secure_cookies": self.security.secure_cookies,
            "session_absolute_ttl_seconds": self.security.session_absolute_ttl_seconds,
            "session_idle_ttl_seconds": self.security.session_idle_ttl_seconds,
            "login_window_seconds": self.security.login_window_seconds,
            "login_max_failures": self.security.login_max_failures,
            "login_lockout_seconds": self.security.login_lockout_seconds,
            "session_pepper_configured": bool(self.security.session_pepper_value),
            "csrf_pepper_configured": bool(self.security.csrf_pepper_value),
            "monitor_token_configured": bool(self.security.monitor_token_value),
            "operator_public_origin": self.security.public_origin,
            "operator_public_host": self.security.public_host,
            "log_format": self.observability.log_format.value,
            "sentry_release": self.observability.release,
            "backend_sentry_configured": bool(self.observability.backend_dsn_value),
            "browser_sentry_configured": bool(self.observability.browser_dsn),
            "sentry_traces_sample_rate": self.observability.traces_sample_rate,
            "sentry_profiles_sample_rate": self.observability.profiles_sample_rate,
            "sentry_send_default_pii": self.observability.send_default_pii,
            "sentry_session_replay_enabled": self.observability.session_replay_enabled,
            "sentry_cron_monitors_configured": bool(self.observability.cron_monitor_slugs),
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
