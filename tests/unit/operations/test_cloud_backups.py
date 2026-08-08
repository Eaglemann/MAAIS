from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from maais.artifacts.bundles import validate_bundle
from maais.artifacts.models import ArtifactRecord, ArtifactType
from maais.artifacts.publisher import ArtifactPublisher, PublicationRequest
from maais.operations.artifact_publication import (
    CloudArtifactIdentity,
    publish_logical_backup_bundle,
)
from maais.operations.backups import (
    BackupMetadata,
    BackupProducerIdentity,
    create_database_backup,
)
from maais.operations.restores import load_verified_backup

DATABASE_URL = (
    "postgresql+psycopg://maais:"
    "local-password@localhost:5432/maais"  # pragma: allowlist secret
)
GENERATED_AT = datetime(2026, 8, 8, 20, tzinfo=timezone.utc)


def _producer() -> BackupProducerIdentity:
    return BackupProducerIdentity(
        artifact_schema_version=1,
        environment="qualification",
        candidate_hash="a" * 64,
        experiment_id=UUID("11111111-1111-4111-8111-111111111111"),
        run_id=UUID("22222222-2222-4222-8222-222222222222"),
        operation_id=UUID("33333333-3333-4333-8333-333333333333"),
        database_system_identifier_sha256="b" * 64,
        railway_deployment_id="deployment-1",
        railway_replica_id="replica-1",
        railway_region="europe-west4-drams3a",
    )


def _metadata(*, producer: BackupProducerIdentity | None) -> BackupMetadata:
    return BackupMetadata(
        database_name="maais",
        schema_revision="0020",
        database_size_bytes=123_456,
        table_counts={"artifact_records": 1, "domain_events": 210},
        ledger={"ok": True, "error_count": 0, "errors": []},
        producer=producer,
    )


def _runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if command[0] == "pg_dump" and "--version" not in command:
        Path(command[command.index("--file") + 1]).write_bytes(b"postgres-custom-archive")
    stdout = "pg_dump (PostgreSQL) 16.14" if "--version" in command else "archive list"
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class CapturingPublisher:
    def __init__(self) -> None:
        self.requests: list[PublicationRequest] = []

    async def publish(self, request: PublicationRequest) -> ArtifactRecord:
        self.requests.append(request)
        return cast(ArtifactRecord, object())


def _artifact_identity() -> CloudArtifactIdentity:
    producer = _producer()
    return CloudArtifactIdentity(
        environment=producer.environment,
        candidate_hash=producer.candidate_hash,
        experiment_id=producer.experiment_id,
        run_id=producer.run_id,
        operation_id=producer.operation_id,
        generated_at=GENERATED_AT,
        producing_deployment_id=producer.railway_deployment_id,
        producing_service_id="operations-1",
    )


def test_cloud_backup_binds_complete_producer_identity_and_validates_as_bundle(
    tmp_path: Path,
) -> None:
    paths = create_database_backup(
        DATABASE_URL,
        tmp_path,
        _metadata(producer=_producer()),
        generated_at=GENERATED_AT,
        runner=_runner,
    )

    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    assert manifest["backup_schema_version"] == 2
    assert manifest["artifact_schema_version"] == 1
    assert manifest["producer"] == _producer().to_json_data()
    assert paths.report_id == manifest["report_id"]
    assert paths.report_id is not None
    assert paths.bundle_manifest_path is not None
    bundle = validate_bundle(paths.directory, expected_report_id=paths.report_id)
    assert {item.relative_path for item in bundle.files} == {
        "backup-manifest.json",
        "bundle-manifest.json",
        "database.dump",
    }
    verified = load_verified_backup(paths.directory)
    assert verified.report_id == paths.report_id


def test_local_backup_contract_remains_schema_one_without_cloud_identity(
    tmp_path: Path,
) -> None:
    paths = create_database_backup(
        DATABASE_URL,
        tmp_path,
        _metadata(producer=None),
        generated_at=GENERATED_AT,
        runner=_runner,
    )

    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    assert manifest["backup_schema_version"] == 1
    assert "producer" not in manifest
    assert paths.report_id is None
    assert paths.bundle_manifest_path is None


def test_backup_producer_identity_rejects_unbound_or_unhashed_values() -> None:
    with pytest.raises(ValueError, match="candidate_hash"):
        replace(_producer(), candidate_hash="invalid")


def test_backup_producer_identity_parser_rejects_non_string_fields() -> None:
    payload = _producer().to_json_data()
    payload["railway_deployment_id"] = 123

    with pytest.raises(ValueError, match="must contain string fields"):
        BackupProducerIdentity.from_json_data(payload)


async def test_cloud_backup_publishes_only_the_locally_verified_logical_bundle(
    tmp_path: Path,
) -> None:
    paths = create_database_backup(
        DATABASE_URL,
        tmp_path,
        _metadata(producer=_producer()),
        generated_at=GENERATED_AT,
        runner=_runner,
    )
    publisher = CapturingPublisher()

    await publish_logical_backup_bundle(
        cast(ArtifactPublisher, publisher),
        paths,
        identity=_artifact_identity(),
    )

    assert len(publisher.requests) == 1
    assert publisher.requests[0].artifact_type is ArtifactType.LOGICAL_BACKUP
    assert publisher.requests[0].report_id == paths.report_id

    with pytest.raises(ValueError, match="producer identity"):
        await publish_logical_backup_bundle(
            cast(ArtifactPublisher, publisher),
            paths,
            identity=replace(
                _artifact_identity(),
                producing_deployment_id="different-deployment",
            ),
        )
    assert len(publisher.requests) == 1

    paths.dump_path.write_bytes(b"tampered-with-same-local-path")
    with pytest.raises(ValueError, match="byte size|SHA-256"):
        await publish_logical_backup_bundle(
            cast(ArtifactPublisher, publisher),
            paths,
            identity=_artifact_identity(),
        )
    assert len(publisher.requests) == 1
