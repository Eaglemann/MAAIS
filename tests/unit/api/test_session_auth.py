from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr, ValidationError

import maais.api.app as app_module
from maais.api.app import create_app
from maais.api.schemas import LoginRequest
from maais.api.security import OperatorPrincipal
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.security.passwords import hash_operator_password

PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret


def _security_settings() -> SecuritySettings:
    return SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(PASSPHRASE)),
        session_pepper=SecretStr("s" * 43),
        csrf_pepper=SecretStr("c" * 43),
        monitor_token=SecretStr("m" * 43),
        secure_cookies=True,
        public_origin="https://mission-control.test",
    )


def test_create_app_uses_injected_security_and_clock_without_global_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> None:
        raise AssertionError("injected app factory read global settings")

    monkeypatch.setattr(app_module, "get_settings", forbidden)

    def clock() -> datetime:
        return datetime(2026, 8, 9, 12, tzinfo=timezone.utc)

    settings = _security_settings()

    application = create_app(
        security_settings=settings,
        clock=clock,
    )

    assert application.state.security.settings is settings
    assert application.state.security.clock is clock


def test_cloud_mode_rejects_local_bearer_configuration() -> None:
    with pytest.raises(ValueError, match="local control token"):
        create_app(
            control_token="a" * 32,
            security_settings=_security_settings(),
        )


def test_login_password_schema_is_secret_safe_bounded_and_forbids_extra_fields() -> None:
    request = LoginRequest(password=PASSPHRASE)

    assert PASSPHRASE not in repr(request)
    assert PASSPHRASE not in str(request.model_dump())
    with pytest.raises(ValidationError):
        LoginRequest(password="x" * 257)
    with pytest.raises(ValidationError):
        LoginRequest.model_validate({"password": PASSPHRASE, "username": "operator"})


def test_operator_principal_is_minimal_and_immutable() -> None:
    principal = OperatorPrincipal(
        actor="sole_operator",
        session_id=None,
        auth_mode=AuthMode.OPERATOR_SESSION,
    )

    assert principal.actor == "sole_operator"
    with pytest.raises((AttributeError, TypeError)):
        principal.actor = "changed"  # type: ignore[misc]
