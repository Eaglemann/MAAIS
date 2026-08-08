from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from maais.config.cloud import DeploymentTarget
from maais.security.passwords import validate_operator_password_hash


class AuthMode(StrEnum):
    LOCAL_TOKEN = "local_token"
    OPERATOR_SESSION = "operator_session"


class SecuritySettings(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    deployment_target: DeploymentTarget = DeploymentTarget.LOCAL
    auth_mode: AuthMode = AuthMode.LOCAL_TOKEN
    operator_password_hash: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    session_pepper: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    csrf_pepper: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    monitor_token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        exclude=True,
        repr=False,
    )
    secure_cookies: bool = True
    public_origin: str = ""
    session_absolute_ttl_seconds: Literal[43_200] = 43_200
    session_idle_ttl_seconds: Literal[1_800] = 1_800
    login_window_seconds: Literal[900] = 900
    login_max_failures: Literal[5] = 5
    login_lockout_seconds: Literal[1_800] = 1_800

    @model_validator(mode="after")
    def validate_authentication_boundary(self) -> Self:
        password_hash = self.operator_password_hash.get_secret_value()
        session_pepper = self.session_pepper.get_secret_value()
        csrf_pepper = self.csrf_pepper.get_secret_value()
        monitor_token = self.monitor_token.get_secret_value()
        secrets = (session_pepper, csrf_pepper, monitor_token)
        if self.deployment_target is DeploymentTarget.RAILWAY and (
            self.auth_mode is not AuthMode.OPERATOR_SESSION
        ):
            raise ValueError("Railway security requires auth_mode=operator_session")
        if self.auth_mode is AuthMode.LOCAL_TOKEN:
            if any((password_hash, *secrets, self.public_origin)):
                raise ValueError("local_token auth forbids inactive operator session secrets")
            return self
        if not password_hash:
            raise ValueError("operator_session requires an Argon2id password hash")
        try:
            validate_operator_password_hash(password_hash)
        except ValueError as error:
            raise ValueError("operator password hash must satisfy the Argon2id policy") from error
        if any(not _high_entropy_secret(value) for value in secrets):
            raise ValueError("operator_session requires three bounded high-entropy secrets")
        if len(set(secrets)) != len(secrets):
            raise ValueError("operator session and monitor secrets must be independent")
        if self.deployment_target is DeploymentTarget.RAILWAY and not self.secure_cookies:
            raise ValueError("Railway operator sessions require secure cookies")
        parsed_origin = urlsplit(self.public_origin)
        try:
            port = parsed_origin.port
        except ValueError as error:
            raise ValueError("operator public origin must be one canonical HTTPS origin") from error
        if (
            not self.public_origin.isascii()
            or self.public_origin != self.public_origin.strip()
            or parsed_origin.scheme != "https"
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or self.public_origin != f"https://{parsed_origin.netloc}"
            or (port is not None and not 1 <= port <= 65_535)
        ):
            raise ValueError("operator public origin must be one canonical HTTPS origin")
        return self

    @property
    def operator_session_configured(self) -> bool:
        return self.auth_mode is AuthMode.OPERATOR_SESSION

    @property
    def operator_password_hash_value(self) -> str:
        return self.operator_password_hash.get_secret_value()

    @property
    def session_pepper_value(self) -> str:
        return self.session_pepper.get_secret_value()

    @property
    def csrf_pepper_value(self) -> str:
        return self.csrf_pepper.get_secret_value()

    @property
    def monitor_token_value(self) -> str:
        return self.monitor_token.get_secret_value()

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_origin).netloc

    def redacted_summary(self) -> dict[str, str | bool | int]:
        return {
            "auth_mode": self.auth_mode.value,
            "operator_session_configured": self.operator_session_configured,
            "secure_cookies": self.secure_cookies,
            "session_absolute_ttl_seconds": self.session_absolute_ttl_seconds,
            "session_idle_ttl_seconds": self.session_idle_ttl_seconds,
            "login_window_seconds": self.login_window_seconds,
            "login_max_failures": self.login_max_failures,
            "login_lockout_seconds": self.login_lockout_seconds,
            "session_pepper_configured": bool(self.session_pepper_value),
            "csrf_pepper_configured": bool(self.csrf_pepper_value),
            "monitor_token_configured": bool(self.monitor_token_value),
            "public_origin": self.public_origin,
            "public_host": self.public_host,
        }


def _high_entropy_secret(value: str) -> bool:
    return value.isascii() and 43 <= len(value) <= 256 and not value.isspace()
