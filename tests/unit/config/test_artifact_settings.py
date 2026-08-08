import json

import pytest
from pydantic import ValidationError

from maais.config.artifacts import (
    ARTIFACT_RETENTION_POLICIES,
    ArtifactSettings,
    ArtifactStoreMode,
    ArtifactType,
    RetentionMode,
    RetentionSettings,
)
from maais.config.cloud import EU_WEST_RAILWAY_REGION, DeploymentTarget, ServiceRole
from maais.config.settings import Settings
from tests.security_support import railway_observability_values, railway_security_values


def _dual_store_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "mode": ArtifactStoreMode.DUAL_S3,
        "replica_endpoint_url": "https://storage.railway.example",
        "replica_region": "auto",
        "replica_bucket": "maais-replica",
        "replica_access_key": "replica-access-canary",  # pragma: allowlist secret
        "replica_secret_key": "replica-secret-canary",  # pragma: allowlist secret
        "canonical_endpoint_url": "https://s3.worm-provider.example",
        "canonical_region": "eu-central-1",
        "canonical_bucket": "maais-canonical",
        "canonical_access_key": "canonical-access-canary",  # pragma: allowlist secret
        "canonical_secret_key": "canonical-secret-canary",  # pragma: allowlist secret
    }
    values.update(overrides)
    return values


def _railway_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        **railway_security_values(),
        **railway_observability_values(ServiceRole.WORKER),
        "deployment_target": DeploymentTarget.RAILWAY,
        "run_mode": "paper_live",
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
        "_env_file": None,
    }
    values.update(overrides)
    return values


def test_local_artifacts_default_to_credential_free_filesystem() -> None:
    artifacts = ArtifactSettings()

    assert artifacts.mode is ArtifactStoreMode.FILESYSTEM
    assert artifacts.local_root.as_posix() == "artifacts"
    assert artifacts.replica_configured is False
    assert artifacts.canonical_configured is False


def test_dual_store_accepts_two_complete_independent_targets() -> None:
    artifacts = ArtifactSettings(**_dual_store_values())

    assert artifacts.mode is ArtifactStoreMode.DUAL_S3
    assert artifacts.replica_configured is True
    assert artifacts.canonical_configured is True
    assert artifacts.canonical_object_lock_required is True


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("canonical_endpoint_url", "https://storage.railway.example"),
        ("canonical_bucket", "maais-replica"),
        ("canonical_access_key", "replica-access-canary"),  # pragma: allowlist secret
        ("canonical_secret_key", "replica-secret-canary"),  # pragma: allowlist secret
    ),
)
def test_official_cloud_storage_requires_independent_targets(
    override: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="independent artifact targets"):
        ArtifactSettings(**_dual_store_values(**{override: value}))


@pytest.mark.parametrize(
    "missing",
    (
        "replica_endpoint_url",
        "replica_region",
        "replica_bucket",
        "replica_access_key",
        "replica_secret_key",
        "canonical_endpoint_url",
        "canonical_region",
        "canonical_bucket",
        "canonical_access_key",
        "canonical_secret_key",
    ),
)
def test_dual_store_requires_every_target_field(missing: str) -> None:
    with pytest.raises(ValidationError, match="complete replica and canonical targets"):
        ArtifactSettings(**_dual_store_values(**{missing: ""}))


def test_canonical_object_lock_requirement_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError):
        ArtifactSettings(
            **_dual_store_values(),
            canonical_object_lock_required=False,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("qualification_days", 29),
        ("operational_days", 91),
        ("official_evidence_days", 366),
    ),
)
def test_retention_periods_are_frozen(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        RetentionSettings(**{field: value})


def test_artifact_retention_policy_is_complete_and_exact() -> None:
    assert ARTIFACT_RETENTION_POLICIES == {
        ArtifactType.QUALIFICATION_WORKING: (RetentionMode.GOVERNANCE, 30),
        ArtifactType.DAILY_REPORT: (RetentionMode.COMPLIANCE, 90),
        ArtifactType.AUDIT_EXPORT: (RetentionMode.COMPLIANCE, 90),
        ArtifactType.LOGICAL_BACKUP: (RetentionMode.COMPLIANCE, 90),
        ArtifactType.MANIFEST: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.QUALIFICATION_EVIDENCE: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.RESTORE_DRILL: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.PROCESS_DRILL: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.PREFLIGHT: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.SOAK_VERDICT: (RetentionMode.COMPLIANCE, 365),
        ArtifactType.FINAL_REPORT: (RetentionMode.COMPLIANCE, 365),
    }
    assert set(ARTIFACT_RETENTION_POLICIES) == set(ArtifactType)


def test_artifact_credentials_never_serialize_or_appear_in_diagnostics() -> None:
    artifacts = ArtifactSettings(
        **_dual_store_values(
            replica_session_token="replica-session-canary",  # pragma: allowlist secret
            canonical_session_token="canonical-session-canary",  # pragma: allowlist secret
        )
    )
    canaries = (
        "replica-access-canary",
        "replica-secret-canary",
        "replica-session-canary",
        "canonical-access-canary",
        "canonical-secret-canary",
        "canonical-session-canary",
    )

    representations = (
        repr(artifacts),
        json.dumps(artifacts.model_dump(mode="json"), sort_keys=True),
        json.dumps(artifacts.redacted_summary(), sort_keys=True),
    )
    for representation in representations:
        for canary in canaries:
            assert canary not in representation


def test_railway_runtime_requires_dual_store_configuration() -> None:
    with pytest.raises(ValidationError, match="dual_s3"):
        Settings(**_railway_values())

    settings = Settings(
        **_railway_values(
            artifact_store_mode=ArtifactStoreMode.DUAL_S3,
            **{
                f"artifact_{name}": value
                for name, value in _dual_store_values().items()
                if name != "mode"
            },
        )
    )
    assert settings.artifacts.mode is ArtifactStoreMode.DUAL_S3


def test_artifact_environment_names_populate_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
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
    }.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.artifacts.mode is ArtifactStoreMode.DUAL_S3
    assert settings.artifacts.replica_bucket == "maais-replica"
    assert settings.artifacts.canonical_bucket == "maais-canonical"


def test_restore_target_url_is_secret_and_absent_from_diagnostics() -> None:
    canary = (
        "postgresql+psycopg://restore:"
        "restore-secret-canary@db.internal:5432/maais_restore_test"  # pragma: allowlist secret
    )
    settings = Settings(_env_file=None, restore_target_database_url=canary)

    assert settings.restore_target_database_url_value == canary
    assert canary not in repr(settings)
    assert canary not in json.dumps(settings.model_dump(mode="json"), sort_keys=True)
    assert canary not in json.dumps(settings.redacted_summary(), sort_keys=True)
