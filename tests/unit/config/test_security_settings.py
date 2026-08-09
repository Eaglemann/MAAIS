from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from maais.config.cloud import DeploymentTarget, ServiceRole
from maais.config.security import AuthMode, SecuritySettings
from maais.security.passwords import hash_operator_password

PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret
HASH = hash_operator_password(PASSPHRASE)


def _railway_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "deployment_target": DeploymentTarget.RAILWAY,
        "service_role": ServiceRole.WEB,
        "auth_mode": AuthMode.OPERATOR_SESSION,
        "operator_password_hash": SecretStr(HASH),
        "session_pepper": SecretStr("s" * 43),
        "csrf_pepper": SecretStr("c" * 43),
        "monitor_token": SecretStr("m" * 43),
        "secure_cookies": True,
        "public_origin": "https://mission-control.test",
    }
    values.update(overrides)
    return values


def test_local_security_defaults_to_credential_free_token_compatibility() -> None:
    settings = SecuritySettings()

    assert settings.auth_mode is AuthMode.LOCAL_TOKEN
    assert settings.deployment_target is DeploymentTarget.LOCAL
    assert settings.operator_session_configured is False


def test_railway_rejects_local_token_auth_and_missing_or_weak_secrets() -> None:
    with pytest.raises(ValidationError, match="operator_session"):
        SecuritySettings(**_railway_values(auth_mode=AuthMode.LOCAL_TOKEN))
    with pytest.raises(ValidationError, match="high-entropy"):
        SecuritySettings(**_railway_values(session_pepper=SecretStr("short")))
    with pytest.raises(ValidationError, match="independent"):
        SecuritySettings(
            **_railway_values(
                csrf_pepper=SecretStr("s" * 43),
            )
        )


@pytest.mark.parametrize(
    "service_role",
    (
        ServiceRole.WORKER,
        ServiceRole.OPERATIONS,
        ServiceRole.VERIFIER,
        ServiceRole.MIGRATOR,
    ),
)
def test_non_web_railway_roles_are_auth_free(service_role: ServiceRole) -> None:
    settings = SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        service_role=service_role,
    )

    assert settings.auth_mode is AuthMode.LOCAL_TOKEN
    assert settings.operator_session_configured is False
    with pytest.raises(ValidationError, match="only the web role"):
        SecuritySettings(**_railway_values(service_role=service_role))


def test_railway_rejects_malformed_hash_and_insecure_cookie() -> None:
    with pytest.raises(ValidationError, match="Argon2id"):
        SecuritySettings(**_railway_values(operator_password_hash=SecretStr("malformed")))
    with pytest.raises(ValidationError, match="secure cookies"):
        SecuritySettings(**_railway_values(secure_cookies=False))


@pytest.mark.parametrize(
    "origin",
    (
        "",
        "http://mission-control.test",
        "https://mission-control.test/",
        "https://user:password@mission-control.test",  # pragma: allowlist secret
        "https://mission-control.test/path",
        "https://mission-control.test?query=value",
    ),
)
def test_operator_origin_is_one_canonical_https_origin(origin: str) -> None:
    with pytest.raises(ValidationError, match="canonical HTTPS origin"):
        SecuritySettings(**_railway_values(public_origin=origin))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("session_absolute_ttl_seconds", 43_201),
        ("session_idle_ttl_seconds", 1_801),
        ("login_window_seconds", 901),
        ("login_max_failures", 6),
        ("login_lockout_seconds", 1_801),
    ),
)
def test_initial_session_and_lockout_policy_is_frozen(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        SecuritySettings(**_railway_values(**{field: value}))


def test_security_secrets_never_serialize_or_appear_in_diagnostics() -> None:
    settings = SecuritySettings(**_railway_values())
    canaries = (HASH, "s" * 43, "c" * 43, "m" * 43)
    rendered = (
        repr(settings),
        json.dumps(settings.model_dump(mode="json"), sort_keys=True),
        json.dumps(settings.redacted_summary(), sort_keys=True),
    )

    assert settings.session_absolute_ttl_seconds == 43_200
    assert settings.session_idle_ttl_seconds == 1_800
    assert settings.login_window_seconds == 900
    assert settings.login_max_failures == 5
    assert settings.login_lockout_seconds == 1_800
    for representation in rendered:
        for canary in canaries:
            assert canary not in representation
