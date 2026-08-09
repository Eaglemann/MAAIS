from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from maais.artifacts.models import ArtifactType
from maais.artifacts.publisher import ArtifactPublisher, PublicationRequest
from maais.db.unit_of_work import UnitOfWork
from maais.operations.backups import (
    BackupMetadata,
    BackupProducerIdentity,
    create_database_backup,
)
from maais.operations.restores import (
    restore_canonical_backup_artifact,
    validate_cloud_restore_target_url,
)
from tests.integration.test_artifact_publisher import (
    MemoryArtifactStore,
    UUIDSequence,
)
from tests.integration.test_artifact_repository import (
    EXPERIMENT_ID,
    NOW,
    OPERATION_ID,
    RUN_ID,
    _acquire_operation,
    _prepare_authority,
)
from tests.integration.test_platform_repository import _descriptor

pytestmark = pytest.mark.integration

SOURCE_URL = (
    "postgresql+psycopg://maais:"
    "source-password@localhost:5432/maais"  # pragma: allowlist secret
)
TARGET_URL = (
    "postgresql+psycopg://restore:"
    "target-password@restore.invalid:5432/maais_cloud_restore_test"  # pragma: allowlist secret
)


def _producer() -> BackupProducerIdentity:
    return BackupProducerIdentity(
        artifact_schema_version=1,
        environment="qualification",
        candidate_hash=_descriptor().descriptor_hash,
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        database_system_identifier_sha256="b" * 64,
        railway_deployment_id="deployment-1",
        railway_replica_id="replica-1",
        railway_region="europe-west4-drams3a",
    )


def _backup_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if command[0] == "pg_dump" and "--version" not in command:
        Path(command[command.index("--file") + 1]).write_bytes(b"postgres-custom-archive")
    stdout = "pg_dump (PostgreSQL) 16.14" if "--version" in command else "archive list"
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


async def test_cloud_restore_downloads_exact_versions_to_private_temporary_bundle(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare_authority(uow_factory)
    await _acquire_operation(uow_factory)
    paths = create_database_backup(
        SOURCE_URL,
        tmp_path / "backups",
        BackupMetadata(
            database_name="maais",
            schema_revision="0022",
            database_size_bytes=123_456,
            table_counts={"artifact_records": 0, "domain_events": 210},
            ledger={"ok": True, "error_count": 0, "errors": []},
            producer=_producer(),
        ),
        generated_at=NOW,
        runner=_backup_runner,
    )
    assert paths.report_id is not None
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)
    publisher = ArtifactPublisher(
        replica=replica,
        canonical=canonical,
        uow_factory=uow_factory,
        now=lambda: NOW + timedelta(minutes=3),
        uuid_factory=UUIDSequence(),
    )
    record = await publisher.publish(
        PublicationRequest(
            bundle_directory=paths.directory,
            environment="qualification",
            candidate_hash=_descriptor().descriptor_hash,
            experiment_id=EXPERIMENT_ID,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            artifact_type=ArtifactType.LOGICAL_BACKUP,
            report_id=paths.report_id,
            generated_at=NOW,
            producing_deployment_id="deployment-1",
            producing_service_id="operations-1",
        )
    )
    commands: list[list[str]] = []
    restored_dump_path: Path | None = None

    def restore_runner(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal restored_dump_path
        commands.append(command)
        if command[0] == "pg_restore":
            restored_dump_path = Path(command[-1])
            assert restored_dump_path.is_file()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    async def collect_restored(_url: str) -> BackupMetadata:
        return BackupMetadata(
            database_name="maais_cloud_restore_test",
            schema_revision="0022",
            database_size_bytes=123_999,
            table_counts={"artifact_records": 0, "domain_events": 210},
            ledger={"ok": True, "error_count": 0, "errors": []},
        )

    verification, passed = await restore_canonical_backup_artifact(
        uow_factory=uow_factory,
        canonical_store=canonical,
        artifact_record_id=record.id,
        target_database_url=TARGET_URL,
        output_directory=tmp_path / "verification",
        runner=restore_runner,
        metadata_collector=collect_restored,
        now=lambda: NOW + timedelta(minutes=4),
    )

    assert passed is True
    assert verification.result_path.is_file()
    assert restored_dump_path is not None and not restored_dump_path.exists()
    assert commands[0][0] == "createdb"
    assert commands[1][0] == "pg_restore"
    assert all("DROP" not in " ".join(command).upper() for command in commands)
    assert "source-password" not in " ".join(part for command in commands for part in command)
    assert "target-password" not in " ".join(part for command in commands for part in command)


def test_cloud_restore_target_is_strictly_fresh_suffix_constrained() -> None:
    assert validate_cloud_restore_target_url("maais", TARGET_URL) == "maais_cloud_restore_test"
    with pytest.raises(ValueError, match="_restore_test"):
        validate_cloud_restore_target_url(
            "maais",
            TARGET_URL.replace("maais_cloud_restore_test", "maais_copy"),
        )
