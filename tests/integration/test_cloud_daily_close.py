from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from maais.artifacts.models import (
    ArtifactPutResult,
    ArtifactType,
    ArtifactWriteRequest,
    ScheduledOperationStatus,
)
from maais.artifacts.publisher import ArtifactPublisher
from maais.artifacts.store import ArtifactStoreError
from maais.db.models.artifacts import ScheduledOperationModel
from maais.db.unit_of_work import UnitOfWork
from maais.operations.artifact_publication import (
    CloudDailyCloseAuthority,
    run_cloud_daily_close,
)
from maais.operations.backups import BackupBundlePaths, BackupMetadata, create_database_backup
from maais.operations.reporting import write_daily_report_bundle
from tests.integration.test_artifact_publisher import (
    MemoryArtifactStore,
    UUIDSequence,
)
from tests.integration.test_artifact_repository import (
    BERLIN_DATE,
    EXPERIMENT_ID,
    NOW,
    OWNER_ONE,
    OWNER_TWO,
    RUN_ID,
    _prepare_authority,
)
from tests.integration.test_platform_repository import _descriptor
from tests.unit.operations.test_reporting import _report

pytestmark = pytest.mark.integration

SOURCE_URL = (
    "postgresql+psycopg://maais:"
    "source-password@localhost:5432/maais"  # pragma: allowlist secret
)
CLOSE_AT = NOW + timedelta(seconds=2)


class SelectiveCanonicalStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__(canonical=True)
        self.fail_logical_backup = False

    async def put_verified(self, request: object) -> ArtifactPutResult:
        assert isinstance(request, ArtifactWriteRequest)
        if self.fail_logical_backup and "/logical_backup/" in request.key:
            raise ArtifactStoreError("simulated canonical backup failure")
        return await super().put_verified(request)


def _authority(
    *,
    owner_boot_id: UUID = OWNER_ONE,
    service_id: str = "operations-1",
) -> CloudDailyCloseAuthority:
    return CloudDailyCloseAuthority(
        environment="qualification",
        candidate_hash=_descriptor().descriptor_hash,
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        owner_boot_id=owner_boot_id,
        database_system_identifier_sha256="b" * 64,
        railway_deployment_id="deployment-1",
        railway_service_id=service_id,
        railway_replica_id=f"replica-{owner_boot_id}",
        railway_region="europe-west4",
    )


def _backup_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if command[0] == "pg_dump" and "--version" not in command:
        Path(command[command.index("--file") + 1]).write_bytes(b"postgres-custom-archive")
    stdout = "pg_dump (PostgreSQL) 16.14" if "--version" in command else "archive list"
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


async def _report_builder(
    experiment_id: UUID,
    report_date: date,
    generated_at: datetime,
) -> dict[str, object]:
    assert experiment_id == EXPERIMENT_ID
    assert report_date == BERLIN_DATE
    assert generated_at == CLOSE_AT
    report = _report()
    report["report_date"] = BERLIN_DATE.isoformat()
    report["generated_at"] = CLOSE_AT.isoformat().replace("+00:00", "Z")
    report["complete_day"] = True
    return report


async def _backup_metadata(database_url: str) -> BackupMetadata:
    assert database_url == SOURCE_URL
    return BackupMetadata(
        database_name="maais",
        schema_revision="0022",
        database_size_bytes=123_456,
        table_counts={"artifact_records": 0, "domain_events": 210},
        ledger={"ok": True, "error_count": 0, "errors": []},
    )


def _backup_builder(
    database_url: str,
    output_directory: Path,
    metadata: BackupMetadata,
    *,
    generated_at: datetime,
) -> BackupBundlePaths:
    return create_database_backup(
        database_url,
        output_directory,
        metadata,
        generated_at=generated_at,
        runner=_backup_runner,
    )


def _publisher(
    uow_factory: UnitOfWork,
    replica: MemoryArtifactStore,
    canonical: MemoryArtifactStore,
    uuid_sequence: UUIDSequence | None = None,
    published_at: datetime = NOW + timedelta(minutes=3),
) -> ArtifactPublisher:
    return ArtifactPublisher(
        replica=replica,
        canonical=canonical,
        uow_factory=uow_factory,
        now=lambda: published_at,
        uuid_factory=uuid_sequence or UUIDSequence(),
    )


async def test_cloud_daily_close_catalogs_exactly_one_report_and_backup_then_resolves_retry(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare_authority(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = SelectiveCanonicalStore()
    report_calls = 0

    async def count_report(
        experiment_id: UUID,
        report_date: date,
        generated_at: datetime,
    ) -> dict[str, object]:
        nonlocal report_calls
        report_calls += 1
        return await _report_builder(experiment_id, report_date, generated_at)

    first = await run_cloud_daily_close(
        authority=_authority(),
        report_date=BERLIN_DATE,
        uow_factory=uow_factory,
        publisher=_publisher(uow_factory, replica, canonical),
        database_url=SOURCE_URL,
        temporary_parent=tmp_path,
        report_builder=count_report,
        report_writer=write_daily_report_bundle,
        backup_metadata_collector=_backup_metadata,
        backup_builder=_backup_builder,
        now=lambda: CLOSE_AT,
        uuid_factory=UUIDSequence(),
    )
    repeated = await run_cloud_daily_close(
        authority=_authority(),
        report_date=BERLIN_DATE,
        uow_factory=uow_factory,
        publisher=_publisher(uow_factory, replica, canonical),
        database_url=SOURCE_URL,
        temporary_parent=tmp_path,
        report_builder=count_report,
        report_writer=write_daily_report_bundle,
        backup_metadata_collector=_backup_metadata,
        backup_builder=_backup_builder,
        now=lambda: NOW + timedelta(hours=1),
        uuid_factory=UUIDSequence(),
    )

    assert first.operation.status is ScheduledOperationStatus.SUCCEEDED
    assert first.operation.generated_at == CLOSE_AT
    assert repeated.operation == first.operation
    assert repeated.report_record == first.report_record
    assert repeated.backup_record == first.backup_record
    assert repeated.resumed is True
    assert report_calls == 1
    assert {first.report_record.artifact_type, first.backup_record.artifact_type} == {
        ArtifactType.DAILY_REPORT,
        ArtifactType.LOGICAL_BACKUP,
    }


async def test_cloud_daily_close_resumes_cataloged_report_after_backup_target_failure(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare_authority(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = SelectiveCanonicalStore()
    canonical.fail_logical_backup = True
    publication_ids = UUIDSequence()
    report_calls = 0

    async def count_report(
        experiment_id: UUID,
        report_date: date,
        generated_at: datetime,
    ) -> dict[str, object]:
        nonlocal report_calls
        report_calls += 1
        return await _report_builder(experiment_id, report_date, generated_at)

    with pytest.raises(Exception, match="canonical_put_failed"):
        await run_cloud_daily_close(
            authority=_authority(),
            report_date=BERLIN_DATE,
            uow_factory=uow_factory,
            publisher=_publisher(uow_factory, replica, canonical, publication_ids),
            database_url=SOURCE_URL,
            temporary_parent=tmp_path,
            report_builder=count_report,
            report_writer=write_daily_report_bundle,
            backup_metadata_collector=_backup_metadata,
            backup_builder=_backup_builder,
            now=lambda: CLOSE_AT,
            uuid_factory=UUIDSequence(),
        )

    async with uow_factory.begin() as uow:
        failed_row = await uow.session.scalar(select(ScheduledOperationModel))
        assert failed_row is not None
        assert failed_row.status == ScheduledOperationStatus.FAILED.value
        records = await uow.artifacts.list_for_operation(failed_row.id)
    assert [record.artifact_type for record in records] == [ArtifactType.DAILY_REPORT]

    async with uow_factory.begin() as uow:
        await uow.platform.stop_service_instance(
            boot_id=OWNER_ONE,
            reason="replacement_after_backup_failure",
            stopped_at=NOW + timedelta(minutes=4),
        )
    canonical.fail_logical_backup = False
    result = await run_cloud_daily_close(
        authority=_authority(owner_boot_id=OWNER_TWO, service_id="operations-2"),
        report_date=BERLIN_DATE,
        uow_factory=uow_factory,
        publisher=_publisher(
            uow_factory,
            replica,
            canonical,
            publication_ids,
            published_at=NOW + timedelta(minutes=6),
        ),
        database_url=SOURCE_URL,
        temporary_parent=tmp_path,
        report_builder=count_report,
        report_writer=write_daily_report_bundle,
        backup_metadata_collector=_backup_metadata,
        backup_builder=_backup_builder,
        now=lambda: NOW + timedelta(minutes=5),
        uuid_factory=UUIDSequence(),
    )

    assert result.operation.status is ScheduledOperationStatus.SUCCEEDED
    assert result.operation.attempt == 2
    assert result.operation.generated_at == CLOSE_AT
    assert result.resumed is True
    assert report_calls == 1
    async with uow_factory.begin() as uow:
        final_records = await uow.artifacts.list_for_operation(result.operation.id)
    assert [record.artifact_type for record in final_records] == [
        ArtifactType.DAILY_REPORT,
        ArtifactType.LOGICAL_BACKUP,
    ]
