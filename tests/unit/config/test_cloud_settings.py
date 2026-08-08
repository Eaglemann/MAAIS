import json

import pytest
from pydantic import ValidationError

from maais.config.cloud import EU_WEST_RAILWAY_REGION, DeploymentTarget, ServiceRole
from maais.config.modes import RunMode
from maais.config.settings import Settings
from tests.security_support import (
    TEST_CSRF_PEPPER,
    TEST_MONITOR_TOKEN,
    TEST_SESSION_PEPPER,
    operator_password_hash_for_tests,
    railway_security_values,
)


def _railway_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        **railway_security_values(),
        "deployment_target": DeploymentTarget.RAILWAY,
        "run_mode": RunMode.PAPER_LIVE,
        "environment": "qualification",
        "service_role": ServiceRole.WORKER,
        "railway_project_id": "project",
        "railway_environment_id": "environment",
        "railway_service_id": "service",
        "railway_deployment_id": "deployment",
        "railway_replica_id": "replica",
        "railway_region": EU_WEST_RAILWAY_REGION,
        "expected_railway_region": EU_WEST_RAILWAY_REGION,
        "railway_git_commit_sha": "a" * 40,
        "expected_schema_revision": "0021",
        "database_role_name": "maais_worker",
        "artifact_store_mode": "dual_s3",
        "artifact_replica_endpoint_url": "https://storage.railway.example",
        "artifact_replica_region": "auto",
        "artifact_replica_bucket": "maais-replica",
        "artifact_replica_access_key": "replica-access",  # pragma: allowlist secret
        "artifact_replica_secret_key": "replica-secret",  # pragma: allowlist secret
        "artifact_canonical_endpoint_url": "https://s3.worm-provider.example",
        "artifact_canonical_region": "eu-central-1",
        "artifact_canonical_bucket": "maais-canonical",
        "artifact_canonical_access_key": "canonical-access",  # pragma: allowlist secret
        "artifact_canonical_secret_key": "canonical-secret",  # pragma: allowlist secret
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_local_settings_keep_existing_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.run_mode is RunMode.REPLAY
    assert settings.deployment_target is DeploymentTarget.LOCAL
    assert settings.service_role is None
    assert settings.cloud.deployment_target is DeploymentTarget.LOCAL
    assert settings.cloud.service_role is None
    assert {role.value for role in ServiceRole} == {
        "web",
        "worker",
        "operations",
        "verifier",
        "migrator",
    }


def test_railway_runtime_rejects_missing_identity_before_startup() -> None:
    with pytest.raises(ValidationError, match="RAILWAY_PROJECT_ID"):
        Settings(
            deployment_target=DeploymentTarget.RAILWAY,
            run_mode=RunMode.PAPER_LIVE,
            service_role=ServiceRole.WORKER,
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("binance_demo_api_key", "configured-key"),  # pragma: allowlist secret
        ("binance_demo_api_secret", "configured-secret"),  # pragma: allowlist secret
    ),
)
def test_railway_paper_runtime_rejects_exchange_credentials(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="exchange credentials"):
        _railway_settings(**{field: value})


@pytest.mark.parametrize(
    ("service_role", "database_role"),
    (
        (ServiceRole.WEB, "maais_web"),
        (ServiceRole.WORKER, "maais_worker"),
        (ServiceRole.OPERATIONS, "maais_ops"),
        (ServiceRole.VERIFIER, "maais_verifier"),
        (ServiceRole.MIGRATOR, "maais_migrator"),
    ),
)
def test_railway_service_role_requires_its_purpose_bound_database_role(
    service_role: ServiceRole,
    database_role: str,
) -> None:
    settings = _railway_settings(
        service_role=service_role,
        database_role_name=database_role,
    )
    assert settings.cloud.database_role_name == database_role

    with pytest.raises(ValidationError, match="database role"):
        _railway_settings(
            service_role=service_role,
            database_role_name="maais_wrong",
        )


def test_railway_application_runtime_requires_paper_live_mode() -> None:
    with pytest.raises(ValidationError, match="paper_live"):
        _railway_settings(run_mode=RunMode.REPLAY)


def test_railway_identity_rejects_values_that_need_trimming() -> None:
    with pytest.raises(ValidationError, match="trimmed"):
        _railway_settings(railway_project_id=" project")


def test_railway_schema_revision_must_be_an_alembic_revision() -> None:
    with pytest.raises(ValidationError, match="schema revision"):
        _railway_settings(expected_schema_revision="head")


def test_railway_runtime_requires_the_frozen_eu_west_region() -> None:
    with pytest.raises(ValidationError, match="expected Railway region"):
        _railway_settings(expected_railway_region="us-west2")
    with pytest.raises(ValidationError, match="unexpected Railway replica region"):
        _railway_settings(railway_region="us-west2")


def test_railway_git_commit_sha_must_be_lowercase_sha1() -> None:
    with pytest.raises(ValidationError, match="Git commit SHA"):
        _railway_settings(railway_git_commit_sha="A" * 40)


def test_railway_candidate_descriptor_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="descriptor path"):
        _railway_settings(candidate_descriptor_path="candidate.json")


def test_redacted_summary_is_an_explicit_non_secret_allowlist() -> None:
    settings = Settings(
        database_url=(
            "postgresql+psycopg://user:db-canary@localhost/maais"  # pragma: allowlist secret
        ),
        binance_demo_api_key="exchange-canary",  # pragma: allowlist secret
        telegram_bot_token="telegram-canary",  # pragma: allowlist secret
        _env_file=None,
    )

    summary = settings.redacted_summary()

    assert summary == {
        "run_mode": "replay",
        "environment": "development",
        "deployment_target": "local",
        "service_role": None,
        "is_production": False,
        "exchange_credentials_configured": True,
        "railway_project_id": "",
        "railway_environment_id": "",
        "railway_service_id": "",
        "railway_deployment_id": "",
        "railway_snapshot_id": None,
        "railway_replica_id": "",
        "railway_region": "",
        "expected_railway_region": "",
        "railway_git_commit_sha": "",
        "expected_schema_revision": "",
        "database_role_name": "",
        "artifact_store_mode": "filesystem",
        "artifact_replica_configured": False,
        "artifact_canonical_configured": False,
        "artifact_canonical_object_lock_required": True,
        "artifact_qualification_retention_days": 30,
        "artifact_operational_retention_days": 90,
        "artifact_official_evidence_retention_days": 365,
        "auth_mode": "local_token",
        "operator_session_configured": False,
        "operator_secure_cookies": True,
        "session_absolute_ttl_seconds": 43_200,
        "session_idle_ttl_seconds": 1_800,
        "login_window_seconds": 900,
        "login_max_failures": 5,
        "login_lockout_seconds": 1_800,
        "session_pepper_configured": False,
        "csrf_pepper_configured": False,
        "monitor_token_configured": False,
    }
    serialized = json.dumps(summary, sort_keys=True)
    assert "db-canary" not in serialized
    assert "exchange-canary" not in serialized
    assert "telegram-canary" not in serialized


def test_settings_representations_exclude_every_secret_field() -> None:
    canaries = (
        "database-secret",
        "test-database-secret",
        "exchange-key-secret",
        "exchange-signing-secret",
        "telegram-token-secret",
        "telegram-chat-secret",
    )
    settings = Settings(
        database_url=(
            "postgresql+psycopg://user:database-secret@localhost/maais"  # pragma: allowlist secret
        ),
        maais_test_database_url=(
            "postgresql+psycopg://user:"
            "test-database-secret@localhost/maais_test"  # pragma: allowlist secret
        ),
        binance_demo_api_key="exchange-key-secret",  # pragma: allowlist secret
        binance_demo_api_secret="exchange-signing-secret",  # pragma: allowlist secret
        telegram_bot_token="telegram-token-secret",  # pragma: allowlist secret
        telegram_chat_id="telegram-chat-secret",  # pragma: allowlist secret
        _env_file=None,
    )

    representations = (
        repr(settings),
        json.dumps(settings.model_dump(mode="json"), sort_keys=True),
    )
    for representation in representations:
        for canary in canaries:
            assert canary not in representation


def test_settings_validation_errors_hide_unparsed_secret_input() -> None:
    canary = "unparsed-database-secret"

    with pytest.raises(ValidationError) as error:
        Settings(database_url=[canary], _env_file=None)

    assert canary not in str(error.value)


def test_railway_builtin_and_maais_environment_names_populate_cloud_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "MAAIS_DEPLOYMENT_TARGET": "railway",
        "MAAIS_SERVICE_ROLE": "worker",
        "RUN_MODE": "paper_live",
        "ENVIRONMENT": "qualification",
        "RAILWAY_PROJECT_ID": "project",
        "RAILWAY_ENVIRONMENT_ID": "environment",
        "RAILWAY_SERVICE_ID": "service",
        "RAILWAY_DEPLOYMENT_ID": "deployment",
        "RAILWAY_REPLICA_ID": "replica",
        "RAILWAY_REPLICA_REGION": EU_WEST_RAILWAY_REGION,
        "MAAIS_EXPECTED_RAILWAY_REGION": EU_WEST_RAILWAY_REGION,
        "RAILWAY_GIT_COMMIT_SHA": "a" * 40,
        "MAAIS_EXPECTED_SCHEMA_REVISION": "0021",
        "MAAIS_DATABASE_ROLE_NAME": "maais_worker",
        "MAAIS_CANDIDATE_DESCRIPTOR_PATH": "/app/candidate.json",
        "MAAIS_AUTH_MODE": "operator_session",
        "MAAIS_OPERATOR_PASSWORD_HASH": operator_password_hash_for_tests(),
        "MAAIS_SESSION_PEPPER": TEST_SESSION_PEPPER,
        "MAAIS_CSRF_PEPPER": TEST_CSRF_PEPPER,
        "MAAIS_MONITOR_TOKEN": TEST_MONITOR_TOKEN,
        "MAAIS_OPERATOR_SECURE_COOKIES": "true",
        "MAAIS_ARTIFACT_STORE_MODE": "dual_s3",
        "MAAIS_ARTIFACT_REPLICA_ENDPOINT_URL": "https://storage.railway.example",
        "MAAIS_ARTIFACT_REPLICA_REGION": "auto",
        "MAAIS_ARTIFACT_REPLICA_BUCKET": "maais-replica",
        "MAAIS_ARTIFACT_REPLICA_ACCESS_KEY": "replica-access",  # pragma: allowlist secret
        "MAAIS_ARTIFACT_REPLICA_SECRET_KEY": "replica-secret",  # pragma: allowlist secret
        "MAAIS_ARTIFACT_CANONICAL_ENDPOINT_URL": "https://s3.worm-provider.example",
        "MAAIS_ARTIFACT_CANONICAL_REGION": "eu-central-1",
        "MAAIS_ARTIFACT_CANONICAL_BUCKET": "maais-canonical",
        "MAAIS_ARTIFACT_CANONICAL_ACCESS_KEY": "canonical-access",  # pragma: allowlist secret
        "MAAIS_ARTIFACT_CANONICAL_SECRET_KEY": "canonical-secret",  # pragma: allowlist secret
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.deployment_target is DeploymentTarget.RAILWAY
    assert settings.service_role is ServiceRole.WORKER
    assert settings.cloud.railway_deployment_id == "deployment"
    assert settings.cloud.railway_region == EU_WEST_RAILWAY_REGION
    assert settings.cloud.expected_railway_region == EU_WEST_RAILWAY_REGION
    assert settings.cloud.railway_git_commit_sha == "a" * 40
    assert settings.cloud.expected_schema_revision == "0021"


def test_railway_environment_is_qualification_or_production_json_runtime() -> None:
    assert _railway_settings(environment="qualification").is_production
    assert _railway_settings(environment="production").is_production

    with pytest.raises(ValidationError, match="deployment environment"):
        _railway_settings(environment="development")
