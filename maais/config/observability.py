"""Role-aware, secret-safe observability configuration."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from maais.config.cloud import DeploymentTarget, ServiceRole

_GIT_RELEASE = re.compile(r"[0-9a-f]{40}")
_MONITOR_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class LogFormat(StrEnum):
    CONSOLE = "console"
    JSON = "json"


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    deployment_target: DeploymentTarget = DeploymentTarget.LOCAL
    service_role: ServiceRole | None = None
    environment: str = "development"
    release: str = ""
    log_format: LogFormat = LogFormat.CONSOLE
    backend_dsn: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    browser_dsn: str = ""
    traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    profiles_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    send_default_pii: Literal[False] = False
    session_replay_enabled: Literal[False] = False
    daily_close_monitor_slug: str = ""
    backup_monitor_slug: str = ""
    evidence_monitor_slug: str = ""

    @model_validator(mode="after")
    def validate_observability_boundary(self) -> Self:
        backend_dsn = self.backend_dsn.get_secret_value()
        dsn_values = (backend_dsn, self.browser_dsn)
        if any(dsn_values) and _GIT_RELEASE.fullmatch(self.release) is None:
            raise ValueError("Sentry release must be exactly 40 lowercase hexadecimal characters")
        if backend_dsn and not _is_canonical_sentry_dsn(backend_dsn):
            raise ValueError("backend Sentry DSN must be one canonical HTTPS DSN")
        if self.browser_dsn and not _is_canonical_sentry_dsn(self.browser_dsn):
            raise ValueError("browser Sentry DSN must be one canonical HTTPS DSN")

        cron_slugs = (
            self.daily_close_monitor_slug,
            self.backup_monitor_slug,
            self.evidence_monitor_slug,
        )
        if any(slug and _MONITOR_SLUG.fullmatch(slug) is None for slug in cron_slugs):
            raise ValueError("Cron names must use a canonical Sentry monitor slug")

        if self.deployment_target is DeploymentTarget.LOCAL:
            return self
        if self.service_role is None:
            raise ValueError("Railway observability requires a service role")
        if self.log_format is not LogFormat.JSON:
            raise ValueError("Railway observability requires JSON logs")
        if self.environment not in {"qualification", "production"}:
            raise ValueError("Railway Sentry environment must be qualification or production")
        if _GIT_RELEASE.fullmatch(self.release) is None:
            raise ValueError("Sentry release must be exactly 40 lowercase hexadecimal characters")
        if not backend_dsn:
            raise ValueError("Railway observability requires a backend Sentry DSN")

        if not self.browser_dsn:
            raise ValueError(
                "Railway images require the public browser Sentry DSN as shared build metadata"
            )

        if self.service_role is ServiceRole.OPERATIONS:
            if not all(cron_slugs):
                raise ValueError("Railway operations role requires all Sentry Cron monitor slugs")
            if len(set(cron_slugs)) != len(cron_slugs):
                raise ValueError("Sentry Cron monitor slugs must be distinct")
        elif any(cron_slugs):
            raise ValueError("only the operations role may receive Sentry Cron monitor slugs")
        return self

    @property
    def backend_dsn_value(self) -> str:
        return self.backend_dsn.get_secret_value()

    @property
    def cron_monitor_slugs(self) -> dict[str, str]:
        if not all(
            (
                self.daily_close_monitor_slug,
                self.backup_monitor_slug,
                self.evidence_monitor_slug,
            )
        ):
            return {}
        return {
            "daily_close": self.daily_close_monitor_slug,
            "backup": self.backup_monitor_slug,
            "evidence": self.evidence_monitor_slug,
        }

    def browser_public_config(self) -> dict[str, str | float]:
        if not self.browser_dsn:
            return {}
        return {
            "dsn": self.browser_dsn,
            "environment": self.environment,
            "release": self.release,
            "traces_sample_rate": self.traces_sample_rate,
        }

    def redacted_summary(self) -> dict[str, str | bool | float | None]:
        return {
            "deployment_target": self.deployment_target.value,
            "service_role": (self.service_role.value if self.service_role is not None else None),
            "environment": self.environment,
            "release": self.release,
            "log_format": self.log_format.value,
            "backend_sentry_configured": bool(self.backend_dsn_value),
            "browser_sentry_configured": bool(self.browser_dsn),
            "traces_sample_rate": self.traces_sample_rate,
            "profiles_sample_rate": self.profiles_sample_rate,
            "send_default_pii": self.send_default_pii,
            "session_replay_enabled": self.session_replay_enabled,
            "cron_monitors_configured": bool(self.cron_monitor_slugs),
        }


def _is_canonical_sentry_dsn(value: str) -> bool:
    if value != value.strip():
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username
        and parsed.password is None
        and parsed.path.strip("/")
        and parsed.query == ""
        and parsed.fragment == ""
    )
