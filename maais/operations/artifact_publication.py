from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from maais.artifacts.bundles import validate_bundle
from maais.artifacts.models import (
    ArtifactRecord,
    ArtifactType,
    ScheduledOperation,
    ScheduledOperationStatus,
    ScheduledOperationType,
    validate_sha256,
)
from maais.artifacts.publisher import ArtifactPublicationError, PublicationRequest
from maais.db.unit_of_work import UnitOfWork
from maais.operations.backups import (
    BackupBundlePaths,
    BackupMetadata,
    BackupProducerIdentity,
    collect_backup_metadata,
    create_database_backup,
)
from maais.operations.reporting import (
    ReportBundlePaths,
    build_configured_daily_report,
    write_daily_report_bundle,
)
from maais.operations.restores import load_verified_backup

UTC = timezone.utc
LOGGER = logging.getLogger(__name__)


class ArtifactPublisherPort(Protocol):
    async def publish(self, request: PublicationRequest) -> ArtifactRecord: ...


class DailyReportBuilder(Protocol):
    async def __call__(
        self,
        experiment_id: UUID,
        report_date: date,
        generated_at: datetime,
    ) -> dict[str, object]: ...


class BackupMetadataCollector(Protocol):
    async def __call__(self, database_url: str) -> BackupMetadata: ...


class BackupBuilder(Protocol):
    def __call__(
        self,
        database_url: str,
        output_directory: Path,
        metadata: BackupMetadata,
        *,
        generated_at: datetime,
    ) -> BackupBundlePaths: ...


@dataclass(frozen=True, slots=True)
class CloudArtifactIdentity:
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    operation_id: UUID
    generated_at: datetime
    producing_deployment_id: str
    producing_service_id: str

    def __post_init__(self) -> None:
        if self.environment not in {"qualification", "production"}:
            raise ValueError("cloud artifact environment must be qualification or production")
        validate_sha256(self.candidate_hash)
        for name, value in (
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
            ("operation_id", self.operation_id),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"cloud artifact {name} must be a non-nil UUID")
        if self.generated_at.tzinfo is not UTC:
            raise ValueError("cloud artifact generated_at must use the UTC timezone")
        for name, value in (
            ("producing_deployment_id", self.producing_deployment_id),
            ("producing_service_id", self.producing_service_id),
        ):
            if not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"cloud artifact {name} must be nonempty and bounded")


@dataclass(frozen=True, slots=True)
class CloudDailyCloseAuthority:
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    owner_boot_id: UUID
    database_system_identifier_sha256: str
    railway_deployment_id: str
    railway_service_id: str
    railway_replica_id: str
    railway_region: str

    def __post_init__(self) -> None:
        if self.environment not in {"qualification", "production"}:
            raise ValueError("cloud daily close environment must be qualification or production")
        validate_sha256(self.candidate_hash)
        validate_sha256(self.database_system_identifier_sha256)
        for name, value in (
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
            ("owner_boot_id", self.owner_boot_id),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"cloud daily close {name} must be a non-nil UUID")
        for name, value in (
            ("railway_deployment_id", self.railway_deployment_id),
            ("railway_service_id", self.railway_service_id),
            ("railway_replica_id", self.railway_replica_id),
            ("railway_region", self.railway_region),
        ):
            if not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"cloud daily close {name} must be nonempty and bounded")


@dataclass(frozen=True, slots=True)
class CloudDailyCloseResult:
    operation: ScheduledOperation
    report_record: ArtifactRecord
    backup_record: ArtifactRecord
    resumed: bool


async def publish_verified_bundle(
    publisher: ArtifactPublisherPort,
    directory: Path,
    *,
    artifact_type: ArtifactType,
    report_id: str,
    identity: CloudArtifactIdentity,
) -> ArtifactRecord:
    """Reject local evidence corruption before invoking any remote storage adapter."""
    await asyncio.to_thread(
        validate_bundle,
        directory,
        expected_report_id=report_id,
    )
    return await publisher.publish(
        PublicationRequest(
            bundle_directory=directory,
            environment=identity.environment,
            candidate_hash=identity.candidate_hash,
            experiment_id=identity.experiment_id,
            run_id=identity.run_id,
            operation_id=identity.operation_id,
            artifact_type=artifact_type,
            report_id=report_id,
            generated_at=identity.generated_at,
            producing_deployment_id=identity.producing_deployment_id,
            producing_service_id=identity.producing_service_id,
        )
    )


async def publish_daily_report_bundle(
    publisher: ArtifactPublisherPort,
    paths: ReportBundlePaths,
    *,
    report_id: str,
    identity: CloudArtifactIdentity,
) -> ArtifactRecord:
    return await publish_verified_bundle(
        publisher,
        paths.directory,
        artifact_type=ArtifactType.DAILY_REPORT,
        report_id=report_id,
        identity=identity,
    )


async def publish_logical_backup_bundle(
    publisher: ArtifactPublisherPort,
    paths: BackupBundlePaths,
    *,
    identity: CloudArtifactIdentity,
) -> ArtifactRecord:
    verified = await asyncio.to_thread(load_verified_backup, paths.directory)
    if verified.report_id is None or paths.report_id != verified.report_id:
        raise ValueError("cloud backup bundle is missing its immutable report identity")
    producer = verified.producer
    if producer is None or not (
        producer.environment == identity.environment
        and producer.candidate_hash == identity.candidate_hash
        and producer.experiment_id == identity.experiment_id
        and producer.run_id == identity.run_id
        and producer.operation_id == identity.operation_id
        and producer.railway_deployment_id == identity.producing_deployment_id
    ):
        raise ValueError("cloud backup producer identity differs from publication authority")
    return await publish_verified_bundle(
        publisher,
        paths.directory,
        artifact_type=ArtifactType.LOGICAL_BACKUP,
        report_id=verified.report_id,
        identity=identity,
    )


async def run_cloud_daily_close(
    *,
    authority: CloudDailyCloseAuthority,
    report_date: date,
    uow_factory: UnitOfWork,
    publisher: ArtifactPublisherPort,
    database_url: str,
    temporary_parent: Path,
    report_builder: DailyReportBuilder | None = None,
    report_writer: Callable[[dict[str, object], Path], ReportBundlePaths] = (
        write_daily_report_bundle
    ),
    backup_metadata_collector: BackupMetadataCollector = collect_backup_metadata,
    backup_builder: BackupBuilder = create_database_backup,
    now: Callable[[], datetime] | None = None,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> CloudDailyCloseResult:
    """Publish one report and one backup under a crash-resumable database operation."""
    clock = now or (lambda: datetime.now(UTC))
    observed_at = _utc_timestamp(clock(), "daily close start")
    candidate = ScheduledOperation.start(
        operation_id=uuid_factory(),
        run_id=authority.run_id,
        experiment_id=authority.experiment_id,
        operation_type=ScheduledOperationType.DAILY_CLOSE,
        berlin_date=report_date,
        owner_boot_id=authority.owner_boot_id,
        generated_at=observed_at,
        started_at=observed_at,
    )
    async with uow_factory.begin() as uow:
        operation = await uow.scheduled_operations.acquire(candidate)

    report_record, backup_record = await _operation_artifacts(
        uow_factory,
        operation,
        authority,
    )
    resumed = operation.attempt > 1 or report_record is not None or backup_record is not None
    if operation.status is ScheduledOperationStatus.SUCCEEDED:
        if report_record is None or backup_record is None:
            raise RuntimeError("successful daily close is missing cataloged evidence")
        return CloudDailyCloseResult(operation, report_record, backup_record, True)

    try:
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="maais-cloud-daily-close-",
            dir=temporary_parent,
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            os.chmod(root, 0o700)
            identity = CloudArtifactIdentity(
                environment=authority.environment,
                candidate_hash=authority.candidate_hash,
                experiment_id=authority.experiment_id,
                run_id=authority.run_id,
                operation_id=operation.id,
                generated_at=operation.generated_at,
                producing_deployment_id=authority.railway_deployment_id,
                producing_service_id=authority.railway_service_id,
            )
            if report_record is None:
                builder = report_builder or _build_configured_report
                report = await builder(
                    authority.experiment_id,
                    report_date,
                    operation.generated_at,
                )
                report_id = _validate_daily_report(
                    report,
                    authority=authority,
                    operation=operation,
                )
                report_paths = await asyncio.to_thread(report_writer, report, root / "reports")
                report_record = await publish_daily_report_bundle(
                    publisher,
                    report_paths,
                    report_id=report_id,
                    identity=identity,
                )
            if backup_record is None:
                metadata = await backup_metadata_collector(database_url)
                producer = BackupProducerIdentity(
                    artifact_schema_version=1,
                    environment=authority.environment,
                    candidate_hash=authority.candidate_hash,
                    experiment_id=authority.experiment_id,
                    run_id=authority.run_id,
                    operation_id=operation.id,
                    database_system_identifier_sha256=(authority.database_system_identifier_sha256),
                    railway_deployment_id=authority.railway_deployment_id,
                    railway_replica_id=authority.railway_replica_id,
                    railway_region=authority.railway_region,
                )
                cloud_metadata = BackupMetadata(
                    database_name=metadata.database_name,
                    schema_revision=metadata.schema_revision,
                    database_size_bytes=metadata.database_size_bytes,
                    table_counts=metadata.table_counts,
                    ledger=metadata.ledger,
                    producer=producer,
                )
                backup_paths = await asyncio.to_thread(
                    backup_builder,
                    database_url,
                    root / "backups",
                    cloud_metadata,
                    generated_at=operation.generated_at,
                )
                backup_record = await publish_logical_backup_bundle(
                    publisher,
                    backup_paths,
                    identity=identity,
                )
        assert report_record is not None
        assert backup_record is not None
        completed_at = _utc_timestamp(clock(), "daily close completion")
        async with uow_factory.begin() as uow:
            completed = await uow.scheduled_operations.complete(
                operation.id,
                owner_boot_id=authority.owner_boot_id,
                result_artifact_ids=(report_record.id, backup_record.id),
                completed_at=completed_at,
            )
        return CloudDailyCloseResult(completed, report_record, backup_record, resumed)
    except Exception as error:
        reason_code = _daily_close_failure_reason(error)
        try:
            async with uow_factory.begin() as uow:
                await uow.scheduled_operations.fail(
                    operation.id,
                    owner_boot_id=authority.owner_boot_id,
                    reason_code=reason_code,
                    failed_at=_utc_timestamp(clock(), "daily close failure"),
                )
        except Exception as persistence_error:
            LOGGER.error(
                "cloud_daily_close_failure_persistence_failed",
                extra={
                    "operation_id": str(operation.id),
                    "reason_code": reason_code,
                    "daily_close_exception_type": type(error).__name__,
                    "persistence_exception_type": type(persistence_error).__name__,
                },
                exc_info=(
                    type(persistence_error),
                    persistence_error,
                    persistence_error.__traceback__,
                ),
            )
        raise


async def _build_configured_report(
    experiment_id: UUID,
    report_date: date,
    generated_at: datetime,
) -> dict[str, object]:
    return await build_configured_daily_report(
        experiment_id,
        report_date,
        generated_at=generated_at,
    )


async def _operation_artifacts(
    uow_factory: UnitOfWork,
    operation: ScheduledOperation,
    authority: CloudDailyCloseAuthority,
) -> tuple[ArtifactRecord | None, ArtifactRecord | None]:
    async with uow_factory.begin() as uow:
        records = await uow.artifacts.list_for_operation(operation.id)
    report: ArtifactRecord | None = None
    backup: ArtifactRecord | None = None
    for record in records:
        if not (
            record.operation_id == operation.id
            and record.environment == authority.environment
            and record.candidate_hash == authority.candidate_hash
            and record.experiment_id == authority.experiment_id
            and record.run_id == authority.run_id
            and record.generated_at == operation.generated_at
        ):
            raise RuntimeError("daily close catalog evidence differs from operation authority")
        if record.artifact_type is ArtifactType.DAILY_REPORT and report is None:
            report = record
        elif record.artifact_type is ArtifactType.LOGICAL_BACKUP and backup is None:
            backup = record
        else:
            raise RuntimeError("daily close operation has duplicate or unexpected evidence")
    if operation.status is ScheduledOperationStatus.SUCCEEDED:
        if report is None or backup is None:
            raise RuntimeError("successful daily close lacks both required artifact records")
        if operation.result_artifact_ids != (report.id, backup.id):
            raise RuntimeError("daily close terminal artifact IDs differ from catalog evidence")
    return report, backup


def _validate_daily_report(
    report: dict[str, object],
    *,
    authority: CloudDailyCloseAuthority,
    operation: ScheduledOperation,
) -> str:
    experiment = report.get("experiment")
    reconciliation = report.get("reconciliation")
    report_id = report.get("report_id")
    if (
        not isinstance(experiment, dict)
        or experiment.get("id") != str(authority.experiment_id)
        or report.get("report_date") != operation.berlin_date.isoformat()
        or report.get("complete_day") is not True
        or not isinstance(reconciliation, dict)
        or reconciliation.get("ledger_ok") is not True
        or reconciliation.get("ledger_error_count") != 0
        or not isinstance(report_id, str)
    ):
        raise ValueError("daily close report is incomplete or differs from operation authority")
    validate_sha256(report_id)
    generated = report.get("generated_at")
    if not isinstance(generated, str):
        raise ValueError("daily close report generated_at is invalid")
    try:
        generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("daily close report generated_at is invalid") from error
    if generated_at.astimezone(UTC) != operation.generated_at:
        raise ValueError("daily close report generated_at differs from frozen operation time")
    return report_id


def _daily_close_failure_reason(error: Exception) -> str:
    if isinstance(error, ArtifactPublicationError):
        return error.reason_code
    if isinstance(error, (ValueError, TypeError)):
        return "daily_close_validation_failed"
    if isinstance(error, OSError):
        return "daily_close_io_failed"
    return "daily_close_failed"


def _utc_timestamp(value: datetime, label: str) -> datetime:
    if value.tzinfo is not UTC:
        raise ValueError(f"{label} must use the UTC timezone")
    return value
