import re
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from maais.config.cloud import (
    DATABASE_ROLE_BY_SERVICE,
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
    railway_region: str = ""
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

    @model_validator(mode="after")
    def validate_railway_identity(self) -> Self:
        if self.deployment_target is DeploymentTarget.LOCAL:
            return self
        required = {
            "RAILWAY_PROJECT_ID": self.railway_project_id,
            "RAILWAY_ENVIRONMENT_ID": self.railway_environment_id,
            "RAILWAY_SERVICE_ID": self.railway_service_id,
            "RAILWAY_DEPLOYMENT_ID": self.railway_deployment_id,
            "RAILWAY_REPLICA_ID": self.railway_replica_id,
            "RAILWAY_REGION": self.railway_region,
            "EXPECTED_SCHEMA_REVISION": self.expected_schema_revision,
            "DATABASE_ROLE_NAME": self.database_role_name,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if self.service_role is None:
            missing.insert(0, "SERVICE_ROLE")
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
            candidate_descriptor_path=self.candidate_descriptor_path,
            expected_schema_revision=self.expected_schema_revision,
            database_role_name=self.database_role_name,
        )

    def redacted_summary(self) -> dict[str, str | bool | None]:
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
            "expected_schema_revision": self.expected_schema_revision,
            "database_role_name": self.database_role_name,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
