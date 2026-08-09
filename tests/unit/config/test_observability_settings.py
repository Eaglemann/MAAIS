from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from maais.config.cloud import DeploymentTarget, ServiceRole
from maais.config.observability import LogFormat, ObservabilitySettings
from maais.config.settings import Settings

BACKEND_DSN = "https://backend-public-key@o0.ingest.sentry.io/123"  # pragma: allowlist secret
BROWSER_DSN = "https://browser-public-key@o0.ingest.sentry.io/456"  # pragma: allowlist secret
RELEASE = "a" * 40


def _railway_values(
    service_role: ServiceRole = ServiceRole.WEB,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "deployment_target": DeploymentTarget.RAILWAY,
        "service_role": service_role,
        "environment": "qualification",
        "release": RELEASE,
        "log_format": LogFormat.JSON,
        "backend_dsn": SecretStr(BACKEND_DSN),
        "browser_dsn": BROWSER_DSN,
        "daily_close_monitor_slug": (
            "maais-qualification-daily-close" if service_role is ServiceRole.OPERATIONS else ""
        ),
        "backup_monitor_slug": (
            "maais-qualification-backup" if service_role is ServiceRole.OPERATIONS else ""
        ),
        "evidence_monitor_slug": (
            "maais-qualification-evidence" if service_role is ServiceRole.OPERATIONS else ""
        ),
    }
    values.update(overrides)
    return values


def test_local_observability_defaults_are_off_and_human_readable() -> None:
    settings = ObservabilitySettings()

    assert settings.deployment_target is DeploymentTarget.LOCAL
    assert settings.service_role is None
    assert settings.log_format is LogFormat.CONSOLE
    assert settings.release == ""
    assert settings.backend_dsn_value == ""
    assert settings.browser_dsn == ""
    assert settings.traces_sample_rate == 0.0
    assert settings.profiles_sample_rate == 0.0
    assert settings.send_default_pii is False
    assert settings.session_replay_enabled is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("log_format", LogFormat.CONSOLE, "JSON logs"),
        ("release", "short", "40 lowercase hexadecimal"),
        ("release", "A" * 40, "40 lowercase hexadecimal"),
        ("environment", "development", "qualification or production"),
        ("backend_dsn", SecretStr(""), "backend Sentry DSN"),
    ),
)
def test_railway_requires_json_exact_release_environment_and_backend_dsn(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ObservabilitySettings(**_railway_values(**{field: value}))


def test_public_browser_dsn_is_shared_build_metadata_for_identical_images() -> None:
    web = ObservabilitySettings(**_railway_values(ServiceRole.WEB))
    worker = ObservabilitySettings(**_railway_values(ServiceRole.WORKER))

    assert web.browser_public_config() == {
        "dsn": BROWSER_DSN,
        "environment": "qualification",
        "release": RELEASE,
        "traces_sample_rate": 0.0,
    }
    assert worker.browser_public_config() == web.browser_public_config()

    with pytest.raises(ValidationError, match="public browser Sentry DSN"):
        ObservabilitySettings(**_railway_values(ServiceRole.WEB, browser_dsn=""))


def test_only_operations_may_receive_distinct_cron_monitor_slugs() -> None:
    operations = ObservabilitySettings(**_railway_values(ServiceRole.OPERATIONS))

    assert operations.cron_monitor_slugs == {
        "daily_close": "maais-qualification-daily-close",
        "backup": "maais-qualification-backup",
        "evidence": "maais-qualification-evidence",
    }

    with pytest.raises(ValidationError, match="operations role requires"):
        ObservabilitySettings(**_railway_values(ServiceRole.OPERATIONS, backup_monitor_slug=""))
    with pytest.raises(ValidationError, match="must be distinct"):
        ObservabilitySettings(
            **_railway_values(
                ServiceRole.OPERATIONS,
                backup_monitor_slug="maais-qualification-daily-close",
            )
        )
    with pytest.raises(ValidationError, match="only the operations role"):
        ObservabilitySettings(
            **_railway_values(
                ServiceRole.WORKER,
                daily_close_monitor_slug="unexpected-monitor",
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("traces_sample_rate", -0.01),
        ("traces_sample_rate", 1.01),
        ("profiles_sample_rate", -0.01),
        ("profiles_sample_rate", 1.01),
        ("send_default_pii", True),
        ("session_replay_enabled", True),
    ),
)
def test_sampling_is_bounded_and_pii_or_replay_cannot_be_enabled(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        ObservabilitySettings(**_railway_values(**{field: value}))


@pytest.mark.parametrize(
    "field",
    (
        "daily_close_monitor_slug",
        "backup_monitor_slug",
        "evidence_monitor_slug",
    ),
)
def test_cron_monitor_slugs_are_canonical(field: str) -> None:
    with pytest.raises(ValidationError, match="canonical Sentry monitor slug"):
        ObservabilitySettings(
            **_railway_values(ServiceRole.OPERATIONS, **{field: "Invalid Monitor"})
        )


def test_backend_dsn_never_serializes_and_browser_config_is_explicit() -> None:
    settings = ObservabilitySettings(**_railway_values(ServiceRole.WEB))
    representations = (
        repr(settings),
        json.dumps(settings.model_dump(mode="json"), sort_keys=True),
        json.dumps(settings.redacted_summary(), sort_keys=True),
        json.dumps(settings.browser_public_config(), sort_keys=True),
    )

    assert settings.backend_dsn_value == BACKEND_DSN
    assert settings.redacted_summary() == {
        "deployment_target": "railway",
        "service_role": "web",
        "environment": "qualification",
        "release": RELEASE,
        "log_format": "json",
        "backend_sentry_configured": True,
        "browser_sentry_configured": True,
        "traces_sample_rate": 0.0,
        "profiles_sample_rate": 0.0,
        "send_default_pii": False,
        "session_replay_enabled": False,
        "cron_monitors_configured": False,
    }
    assert BROWSER_DSN in representations[-1]
    for representation in representations:
        assert BACKEND_DSN not in representation


def test_root_settings_exposes_only_redacted_observability_state() -> None:
    settings = Settings(
        service_role=ServiceRole.WEB,
        railway_git_commit_sha=RELEASE,
        log_format=LogFormat.JSON,
        sentry_backend_dsn=BACKEND_DSN,
        sentry_browser_dsn=BROWSER_DSN,
        _env_file=None,
    )

    assert settings.observability.backend_dsn_value == BACKEND_DSN
    assert settings.observability.browser_dsn == BROWSER_DSN
    serialized = json.dumps(settings.redacted_summary(), sort_keys=True)
    assert BACKEND_DSN not in serialized
    assert "backend_sentry_configured" in serialized
