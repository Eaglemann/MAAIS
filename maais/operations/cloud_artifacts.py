from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from maais.artifacts.configured import build_configured_artifact_runtime
from maais.artifacts.models import (
    ArtifactRecord,
    ArtifactType,
    ScheduledOperation,
    ScheduledOperationStatus,
    ScheduledOperationType,
)
from maais.artifacts.publisher import ArtifactPublicationError
from maais.config.cloud import ServiceRole
from maais.config.settings import Settings
from maais.db.unit_of_work import UnitOfWork
from maais.operations.artifact_publication import (
    CloudArtifactIdentity,
    CloudDailyCloseAuthority,
    CloudDailyCloseResult,
    publish_logical_backup_bundle,
    publish_verified_bundle,
    run_cloud_daily_close,
)
from maais.operations.backups import (
    BackupMetadata,
    BackupProducerIdentity,
    collect_backup_metadata,
    create_database_backup,
)
from maais.operations.restores import restore_canonical_backup_artifact
from maais.platform.runtime import RuntimeIdentityEvidence, verify_configured_runtime_identity

UTC = timezone.utc
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CloudOperationResult:
    operation: ScheduledOperation
    artifact: ArtifactRecord
    resumed: bool

    def to_json_data(self) -> dict[str, object]:
        return {
            "artifact_record_id": str(self.artifact.id),
            "artifact_type": self.artifact.artifact_type.value,
            "canonical_versions": [item.version_id for item in self.artifact.canonical_inventory],
            "operation_id": str(self.operation.id),
            "report_id": self.artifact.report_id,
            "resumed": self.resumed,
            "status": self.operation.status.value,
        }


async def publish_configured_cloud_bundle(
    *,
    settings: Settings,
    run_id: UUID,
    experiment_id: UUID,
    report_date: date,
    artifact_type: ArtifactType,
    report_id: str,
    bundle_directory: Path,
) -> CloudOperationResult:
    evidence = await _operations_evidence(settings, run_id)
    runtime = build_configured_artifact_runtime(settings)
    operation: ScheduledOperation | None = None
    try:
        operation = await _acquire_operation(
            runtime.uow_factory,
            evidence=evidence,
            experiment_id=experiment_id,
            run_id=run_id,
            report_date=report_date,
            operation_type=ScheduledOperationType.ARTIFACT_PUBLICATION,
        )
        existing = await _resolve_single_artifact(
            runtime.uow_factory,
            operation=operation,
            evidence=evidence,
            environment=settings.environment,
            artifact_type=artifact_type,
            report_id=report_id,
        )
        if existing is not None:
            completed = await _complete_single(runtime.uow_factory, operation, evidence, existing)
            return CloudOperationResult(completed, existing, True)
        record = await publish_verified_bundle(
            runtime.publisher,
            bundle_directory,
            artifact_type=artifact_type,
            report_id=report_id,
            identity=_artifact_identity(settings, evidence, operation, experiment_id, run_id),
        )
        completed = await _complete_single(runtime.uow_factory, operation, evidence, record)
        return CloudOperationResult(completed, record, operation.attempt > 1)
    except Exception as error:
        if operation is not None and operation.status is ScheduledOperationStatus.RUNNING:
            await _fail_operation(runtime.uow_factory, operation, evidence, error)
        raise
    finally:
        await runtime.close()


async def backup_configured_cloud_database(
    *,
    settings: Settings,
    run_id: UUID,
    experiment_id: UUID,
    report_date: date,
    temporary_parent: Path,
) -> CloudOperationResult:
    evidence = await _operations_evidence(settings, run_id)
    runtime = build_configured_artifact_runtime(settings)
    operation: ScheduledOperation | None = None
    try:
        operation = await _acquire_operation(
            runtime.uow_factory,
            evidence=evidence,
            experiment_id=experiment_id,
            run_id=run_id,
            report_date=report_date,
            operation_type=ScheduledOperationType.LOGICAL_BACKUP,
        )
        existing = await _resolve_single_artifact(
            runtime.uow_factory,
            operation=operation,
            evidence=evidence,
            environment=settings.environment,
            artifact_type=ArtifactType.LOGICAL_BACKUP,
            report_id=None,
        )
        if existing is not None:
            completed = await _complete_single(runtime.uow_factory, operation, evidence, existing)
            return CloudOperationResult(completed, existing, True)
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="maais-cloud-backup-",
            dir=temporary_parent,
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            os.chmod(root, 0o700)
            metadata = await collect_backup_metadata(settings.database_url_value)
            cloud_metadata = BackupMetadata(
                database_name=metadata.database_name,
                schema_revision=metadata.schema_revision,
                database_size_bytes=metadata.database_size_bytes,
                table_counts=metadata.table_counts,
                ledger=metadata.ledger,
                producer=BackupProducerIdentity(
                    artifact_schema_version=1,
                    environment=settings.environment,
                    candidate_hash=evidence.identity.candidate_hash,
                    experiment_id=experiment_id,
                    run_id=run_id,
                    operation_id=operation.id,
                    database_system_identifier_sha256=(evidence.database_system_identifier_sha256),
                    railway_deployment_id=evidence.identity.deployment_id,
                    railway_replica_id=evidence.identity.replica_id,
                    railway_region=evidence.identity.region,
                ),
            )
            paths = await asyncio.to_thread(
                create_database_backup,
                settings.database_url_value,
                root,
                cloud_metadata,
                generated_at=operation.generated_at,
            )
            record = await publish_logical_backup_bundle(
                runtime.publisher,
                paths,
                identity=_artifact_identity(
                    settings,
                    evidence,
                    operation,
                    experiment_id,
                    run_id,
                ),
            )
        completed = await _complete_single(runtime.uow_factory, operation, evidence, record)
        return CloudOperationResult(completed, record, operation.attempt > 1)
    except Exception as error:
        if operation is not None and operation.status is ScheduledOperationStatus.RUNNING:
            await _fail_operation(runtime.uow_factory, operation, evidence, error)
        raise
    finally:
        await runtime.close()


async def close_configured_cloud_day(
    *,
    settings: Settings,
    run_id: UUID,
    experiment_id: UUID,
    report_date: date,
    temporary_parent: Path,
) -> CloudDailyCloseResult:
    evidence = await _operations_evidence(settings, run_id)
    runtime = build_configured_artifact_runtime(settings)
    try:
        return await run_cloud_daily_close(
            authority=CloudDailyCloseAuthority(
                environment=settings.environment,
                candidate_hash=evidence.identity.candidate_hash,
                experiment_id=experiment_id,
                run_id=run_id,
                owner_boot_id=evidence.identity.boot_id,
                database_system_identifier_sha256=(evidence.database_system_identifier_sha256),
                railway_deployment_id=evidence.identity.deployment_id,
                railway_service_id=evidence.identity.service_id,
                railway_replica_id=evidence.identity.replica_id,
                railway_region=evidence.identity.region,
            ),
            report_date=report_date,
            uow_factory=runtime.uow_factory,
            publisher=runtime.publisher,
            database_url=settings.database_url_value,
            temporary_parent=temporary_parent,
        )
    finally:
        await runtime.close()


async def restore_configured_cloud_backup(
    *,
    settings: Settings,
    artifact_record_id: UUID,
    output_directory: Path,
) -> dict[str, object]:
    _require_operations_role(settings)
    target_url = settings.restore_target_database_url_value
    if not target_url:
        raise ValueError("MAAIS_RESTORE_TARGET_DATABASE_URL is required")
    runtime = build_configured_artifact_runtime(settings)
    try:
        async with runtime.uow_factory.begin() as uow:
            source = await uow.artifacts.get_record(artifact_record_id)
        evidence = await verify_configured_runtime_identity(
            settings=settings,
            run_id=source.run_id,
        )
        if (
            source.artifact_type is not ArtifactType.LOGICAL_BACKUP
            or source.candidate_hash != evidence.identity.candidate_hash
        ):
            raise ValueError("restore source differs from verified runtime authority")
        paths, passed = await restore_canonical_backup_artifact(
            uow_factory=runtime.uow_factory,
            canonical_store=runtime.canonical_store,
            artifact_record_id=artifact_record_id,
            target_database_url=target_url,
            output_directory=output_directory,
        )
        return {
            "artifact_record_id": str(artifact_record_id),
            "passed": passed,
            "verification": str(paths.result_path),
        }
    finally:
        await runtime.close()


async def _operations_evidence(settings: Settings, run_id: UUID) -> RuntimeIdentityEvidence:
    _require_operations_role(settings)
    return await verify_configured_runtime_identity(settings=settings, run_id=run_id)


def _require_operations_role(settings: Settings) -> None:
    if settings.service_role is not ServiceRole.OPERATIONS:
        raise ValueError("cloud artifact operations require MAAIS_SERVICE_ROLE=operations")


async def _acquire_operation(
    uow_factory: UnitOfWork,
    *,
    evidence: RuntimeIdentityEvidence,
    experiment_id: UUID,
    run_id: UUID,
    report_date: date,
    operation_type: ScheduledOperationType,
) -> ScheduledOperation:
    observed_at = datetime.now(UTC)
    candidate = ScheduledOperation.start(
        operation_id=uuid4(),
        run_id=run_id,
        experiment_id=experiment_id,
        operation_type=operation_type,
        berlin_date=report_date,
        owner_boot_id=evidence.identity.boot_id,
        generated_at=observed_at,
        started_at=observed_at,
    )
    async with uow_factory.begin() as uow:
        return await uow.scheduled_operations.acquire(candidate)


async def _resolve_single_artifact(
    uow_factory: UnitOfWork,
    *,
    operation: ScheduledOperation,
    evidence: RuntimeIdentityEvidence,
    environment: str,
    artifact_type: ArtifactType,
    report_id: str | None,
) -> ArtifactRecord | None:
    async with uow_factory.begin() as uow:
        records = await uow.artifacts.list_for_operation(operation.id)
    if len(records) > 1:
        raise RuntimeError("single-artifact operation has multiple catalog records")
    record = records[0] if records else None
    if record is not None and not (
        record.operation_id == operation.id
        and record.environment == environment
        and record.candidate_hash == evidence.identity.candidate_hash
        and record.experiment_id == operation.experiment_id
        and record.run_id == operation.run_id
        and record.artifact_type is artifact_type
        and record.generated_at == operation.generated_at
        and (report_id is None or record.report_id == report_id)
    ):
        raise RuntimeError("single-artifact catalog evidence differs from operation authority")
    if operation.status is ScheduledOperationStatus.SUCCEEDED:
        if record is None or operation.result_artifact_ids != (record.id,):
            raise RuntimeError("successful operation terminal evidence differs from catalog")
    return record


async def _complete_single(
    uow_factory: UnitOfWork,
    operation: ScheduledOperation,
    evidence: RuntimeIdentityEvidence,
    record: ArtifactRecord,
) -> ScheduledOperation:
    if operation.status is ScheduledOperationStatus.SUCCEEDED:
        return operation
    async with uow_factory.begin() as uow:
        return await uow.scheduled_operations.complete(
            operation.id,
            owner_boot_id=evidence.identity.boot_id,
            result_artifact_ids=(record.id,),
            completed_at=datetime.now(UTC),
        )


async def _fail_operation(
    uow_factory: UnitOfWork,
    operation: ScheduledOperation,
    evidence: RuntimeIdentityEvidence,
    error: Exception,
) -> None:
    reason_code = (
        error.reason_code
        if isinstance(error, ArtifactPublicationError)
        else "cloud_artifact_operation_failed"
    )
    try:
        async with uow_factory.begin() as uow:
            await uow.scheduled_operations.fail(
                operation.id,
                owner_boot_id=evidence.identity.boot_id,
                reason_code=reason_code,
                failed_at=datetime.now(UTC),
            )
    except Exception as persistence_error:
        LOGGER.error(
            "cloud_artifact_operation_failure_persistence_failed",
            extra={
                "operation_id": str(operation.id),
                "operation_exception_type": type(error).__name__,
                "persistence_exception_type": type(persistence_error).__name__,
                "reason_code": reason_code,
            },
            exc_info=(
                type(persistence_error),
                persistence_error,
                persistence_error.__traceback__,
            ),
        )


def _artifact_identity(
    settings: Settings,
    evidence: RuntimeIdentityEvidence,
    operation: ScheduledOperation,
    experiment_id: UUID,
    run_id: UUID,
) -> CloudArtifactIdentity:
    return CloudArtifactIdentity(
        environment=settings.environment,
        candidate_hash=evidence.identity.candidate_hash,
        experiment_id=experiment_id,
        run_id=run_id,
        operation_id=operation.id,
        generated_at=operation.generated_at,
        producing_deployment_id=evidence.identity.deployment_id,
        producing_service_id=evidence.identity.service_id,
    )
