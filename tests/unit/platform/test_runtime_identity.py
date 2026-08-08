from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from maais.cli import build_parser, main
from maais.config.cloud import EU_WEST_RAILWAY_REGION, DeploymentTarget, ServiceRole
from maais.config.settings import Settings
from maais.platform.identity import CandidateDescriptor
from maais.platform.runtime import (
    RuntimeDatabaseIdentity,
    RuntimeIdentityError,
    RuntimeIdentityEvidence,
    build_runtime_identity_evidence,
    process_boot_identity,
)
from tests.unit.platform.test_registry_domain import _descriptor

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
BOOT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SYSTEM_IDENTIFIER = "7669409277984608290"


def _settings(tmp_path: Path, descriptor: CandidateDescriptor, **overrides: object) -> Settings:
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(descriptor.to_json_data()), encoding="utf-8")
    values: dict[str, object] = {
        "deployment_target": DeploymentTarget.RAILWAY,
        "run_mode": "paper_live",
        "environment": "qualification",
        "service_role": ServiceRole.WORKER,
        "railway_project_id": "project-1",
        "railway_environment_id": "environment-1",
        "railway_service_id": "worker-service",
        "railway_deployment_id": "deployment-1",
        "railway_snapshot_id": "snapshot-1",
        "railway_replica_id": "replica-1",
        "railway_region": EU_WEST_RAILWAY_REGION,
        "expected_railway_region": EU_WEST_RAILWAY_REGION,
        "railway_git_commit_sha": descriptor.git_sha,
        "candidate_descriptor_path": candidate_path,
        "expected_schema_revision": descriptor.schema_revision,
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


def _database(**overrides: object) -> RuntimeDatabaseIdentity:
    values: dict[str, object] = {
        "current_user": "maais_worker",
        "schema_revision": "0019",
        "system_identifier": SYSTEM_IDENTIFIER,
    }
    values.update(overrides)
    return RuntimeDatabaseIdentity(**values)  # type: ignore[arg-type]


def test_runtime_identity_evidence_is_exact_and_secret_free(tmp_path: Path) -> None:
    descriptor = _descriptor()
    settings = _settings(tmp_path, descriptor)

    evidence = build_runtime_identity_evidence(
        settings=settings,
        descriptor=descriptor,
        descriptor_from_image=CandidateDescriptor.from_path(settings.candidate_descriptor_path),
        database=_database(),
        boot_id=BOOT_ID,
        started_at=NOW,
    )

    assert evidence.identity.to_json_data() == {
        "boot_id": str(BOOT_ID),
        "candidate_hash": descriptor.descriptor_hash,
        "deployment_id": "deployment-1",
        "environment_id": "environment-1",
        "project_id": "project-1",
        "region": EU_WEST_RAILWAY_REGION,
        "replica_id": "replica-1",
        "service_id": "worker-service",
        "service_role": "worker",
        "snapshot_id": "snapshot-1",
        "started_at": "2026-08-08T12:00:00Z",
    }
    assert evidence.to_json_data() == {
        "boot_id": str(BOOT_ID),
        "candidate_hash": descriptor.descriptor_hash,
        "database_system_identifier_sha256": hashlib.sha256(
            SYSTEM_IDENTIFIER.encode("ascii")
        ).hexdigest(),
        "deployment_id": "deployment-1",
        "region": EU_WEST_RAILWAY_REGION,
        "replica_id": "replica-1",
        "role": "worker",
        "schema_revision": "0019",
    }
    serialized = json.dumps(evidence.to_json_data(), sort_keys=True)
    assert "postgres" not in serialized
    assert "DATABASE_URL" not in serialized


@pytest.mark.parametrize(
    ("settings_overrides", "database_overrides", "descriptor_change", "message"),
    (
        ({"railway_git_commit_sha": "f" * 40}, {}, None, "Git commit"),
        ({}, {"current_user": "maais_web"}, None, "database role"),
        ({}, {"schema_revision": "0018"}, None, "schema"),
        ({}, {"system_identifier": "not-a-system-id"}, None, "system identifier"),
        ({}, {}, {"build_definition_sha256": "f" * 64}, "candidate descriptor"),
    ),
)
def test_runtime_identity_fails_closed_on_every_identity_mismatch(
    tmp_path: Path,
    settings_overrides: dict[str, object],
    database_overrides: dict[str, object],
    descriptor_change: dict[str, object] | None,
    message: str,
) -> None:
    descriptor = _descriptor()
    settings = _settings(tmp_path, descriptor, **settings_overrides)
    descriptor_from_image = CandidateDescriptor.from_path(settings.candidate_descriptor_path)
    if descriptor_change is not None:
        changed_values = {
            **descriptor.to_json_data(),
            **descriptor_change,
        }
        changed_values.pop("descriptor_hash")
        changed_values.pop("schema_version")
        descriptor_from_image = CandidateDescriptor.build(**changed_values)  # type: ignore[arg-type]

    with pytest.raises(RuntimeIdentityError, match=message):
        build_runtime_identity_evidence(
            settings=settings,
            descriptor=descriptor,
            descriptor_from_image=descriptor_from_image,
            database=_database(**database_overrides),
            boot_id=BOOT_ID,
            started_at=NOW,
        )


def test_process_boot_identity_is_generated_once_and_not_cli_controlled() -> None:
    first = process_boot_identity()
    second = process_boot_identity()
    parser = build_parser()
    arguments = parser.parse_args(["cloud-identity", "--json"])

    assert first is second
    assert first.boot_id.int != 0
    assert first.started_at.tzinfo is timezone.utc
    assert arguments.json is True
    assert not hasattr(arguments, "boot_id")
    with pytest.raises(SystemExit):
        parser.parse_args(["cloud-identity", "--json", "--boot-id", str(BOOT_ID)])


def test_cloud_identity_cli_prints_only_the_public_attestation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    evidence = RuntimeIdentityEvidence(
        identity=build_runtime_identity_evidence(
            settings=_settings(tmp_path, _descriptor()),
            descriptor=_descriptor(),
            descriptor_from_image=_descriptor(),
            database=_database(),
            boot_id=BOOT_ID,
            started_at=NOW,
        ).identity,
        schema_revision="0019",
        database_system_identifier_sha256="f" * 64,
    )

    async def fake_identity(*, settings: Settings) -> RuntimeIdentityEvidence:
        assert isinstance(settings, Settings)
        return evidence

    monkeypatch.setattr("maais.cli.verify_configured_runtime_identity", fake_identity)
    monkeypatch.setattr("maais.cli.get_settings", lambda: Settings(_env_file=None))

    assert main(["cloud-identity", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == evidence.to_json_data()
