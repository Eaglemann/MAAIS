import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from maais.config.cloud import EU_WEST_RAILWAY_REGION, DeploymentTarget, ServiceRole
from maais.config.modes import RunMode
from maais.config.settings import Settings
from tests.security_support import (
    railway_observability_values,
    railway_security_values,
)


def _railway_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
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
        "expected_schema_revision": "0022",
        "database_role_name": "maais_worker",
        "_env_file": None,
    }
    values.update(overrides)
    service_role = ServiceRole(values["service_role"])
    if service_role is ServiceRole.WEB:
        for name, value in railway_security_values().items():
            values.setdefault(name, value)
    if service_role is ServiceRole.WORKER:
        artifact_values = {
            "artifact_store_mode": "canonical_read",
            "artifact_canonical_endpoint_url": "https://s3.worm-provider.example",
            "artifact_canonical_region": "eu-central-1",
            "artifact_canonical_bucket": "maais-canonical",
            "artifact_canonical_access_key": "canonical-read-access",  # pragma: allowlist secret
            "artifact_canonical_secret_key": "canonical-read-secret",  # pragma: allowlist secret
        }
        for name, value in artifact_values.items():
            values.setdefault(name, value)
    if service_role is ServiceRole.OPERATIONS:
        artifact_values = {
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
        }
        for name, value in artifact_values.items():
            values.setdefault(name, value)
    if service_role in {ServiceRole.WORKER, ServiceRole.OPERATIONS, ServiceRole.VERIFIER}:
        values.setdefault("cloud_run_id", UUID("11111111-1111-4111-8111-111111111111"))
    if service_role is ServiceRole.WORKER:
        values.setdefault(
            "manifest_artifact_id",
            UUID("22222222-2222-4222-8222-222222222222"),
        )
    for name, value in railway_observability_values(service_role).items():
        values.setdefault(name, value)
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


def test_railway_non_web_roles_reject_operator_authentication_secrets() -> None:
    with pytest.raises(ValidationError, match="only the web role"):
        _railway_settings(
            service_role=ServiceRole.WORKER,
            database_role_name="maais_worker",
            **railway_security_values(),
        )


def test_railway_roles_without_artifact_authority_reject_store_credentials() -> None:
    with pytest.raises(ValidationError, match="artifact authority"):
        _railway_settings(
            service_role=ServiceRole.VERIFIER,
            database_role_name="maais_verifier",
            artifact_store_mode="dual_s3",
            artifact_replica_endpoint_url="https://storage.railway.example",
            artifact_replica_region="auto",
            artifact_replica_bucket="maais-replica",
            artifact_replica_access_key="replica-access",  # pragma: allowlist secret
            artifact_replica_secret_key="replica-secret",  # pragma: allowlist secret
            artifact_canonical_endpoint_url="https://s3.worm-provider.example",
            artifact_canonical_region="eu-central-1",
            artifact_canonical_bucket="maais-canonical",
            artifact_canonical_access_key="canonical-access",  # pragma: allowlist secret
            artifact_canonical_secret_key="canonical-secret",  # pragma: allowlist secret
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


@pytest.mark.parametrize("field", ("cloud_run_id", "manifest_artifact_id"))
def test_railway_runtime_rejects_nil_catalog_identifiers(field: str) -> None:
    with pytest.raises(ValidationError, match="non-nil UUID"):
        _railway_settings(**{field: UUID(int=0)})


def test_railway_catalog_identifiers_are_role_scoped() -> None:
    with pytest.raises(ValidationError, match="web and migrator roles forbid run identity"):
        _railway_settings(
            service_role=ServiceRole.WEB,
            database_role_name="maais_web",
            cloud_run_id=UUID("11111111-1111-4111-8111-111111111111"),
        )
    with pytest.raises(ValidationError, match="only the worker role"):
        _railway_settings(
            service_role=ServiceRole.OPERATIONS,
            database_role_name="maais_ops",
            manifest_artifact_id=UUID("22222222-2222-4222-8222-222222222222"),
        )


def test_redacted_summary_is_an_explicit_non_secret_allowlist() -> None:
    settings = Settings(
        database_url=(
            "postgresql+psycopg://user:db-canary@localhost/maais"  # pragma: allowlist secret
        ),
        binance_demo_api_key="exchange-canary",  # pragma: allowlist secret
        telegram_bot_token="telegram-canary",  # pragma: allowlist secret
        log_format="console",
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
        "cloud_run_id": None,
        "manifest_artifact_id": None,
        "port": 8000,
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
        "operator_public_origin": "",
        "operator_public_host": "",
        "log_format": "console",
        "sentry_release": "",
        "backend_sentry_configured": False,
        "browser_sentry_configured": False,
        "sentry_traces_sample_rate": 0.0,
        "sentry_profiles_sample_rate": 0.0,
        "sentry_send_default_pii": False,
        "sentry_session_replay_enabled": False,
        "sentry_cron_monitors_configured": False,
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
        "backend-sentry-secret",
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
        sentry_backend_dsn=(
            "https://backend-sentry-secret@o0.ingest.sentry.io/789"  # pragma: allowlist secret
        ),
        railway_git_commit_sha="a" * 40,
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
        "MAAIS_EXPECTED_SCHEMA_REVISION": "0022",
        "MAAIS_DATABASE_ROLE_NAME": "maais_worker",
        "MAAIS_RUN_ID": "11111111-1111-4111-8111-111111111111",
        "MAAIS_MANIFEST_ARTIFACT_ID": "22222222-2222-4222-8222-222222222222",
        "PORT": "12345",
        "MAAIS_LOG_FORMAT": "json",
        "SENTRY_DSN": (
            "https://backend-public-key@o0.ingest.sentry.io/123"  # pragma: allowlist secret
        ),
        "MAAIS_CANDIDATE_DESCRIPTOR_PATH": "/app/candidate.json",
        "VITE_SENTRY_DSN": (
            "https://browser-public-key@o0.ingest.sentry.io/456"  # pragma: allowlist secret
        ),
        "MAAIS_ARTIFACT_STORE_MODE": "canonical_read",
        "MAAIS_ARTIFACT_CANONICAL_ENDPOINT_URL": "https://s3.worm-provider.example",
        "MAAIS_ARTIFACT_CANONICAL_REGION": "eu-central-1",
        "MAAIS_ARTIFACT_CANONICAL_BUCKET": "maais-canonical",
        "MAAIS_ARTIFACT_CANONICAL_ACCESS_KEY": "canonical-read-access",  # pragma: allowlist secret
        "MAAIS_ARTIFACT_CANONICAL_SECRET_KEY": "canonical-read-secret",  # pragma: allowlist secret
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
    assert settings.cloud.expected_schema_revision == "0022"
    assert settings.cloud_run_id == UUID("11111111-1111-4111-8111-111111111111")
    assert settings.manifest_artifact_id == UUID("22222222-2222-4222-8222-222222222222")
    assert settings.port == 12_345


def test_railway_environment_is_qualification_or_production_json_runtime() -> None:
    assert _railway_settings(environment="qualification").is_production
    assert _railway_settings(environment="production").is_production

    with pytest.raises(ValidationError, match="deployment environment"):
        _railway_settings(environment="development")
