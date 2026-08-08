"""Append-only artifact publication attempts and tamper-evident catalog."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.artifacts.models import (
    GENESIS_EVIDENCE_HASH,
    ArtifactPublicationAttempt,
    ArtifactRecord,
    ArtifactType,
    PublicationAttemptStatus,
    stored_artifact_from_json,
    stored_artifact_json,
)
from maais.db.models.artifacts import (
    ArtifactPublicationAttemptModel,
    ArtifactRecordModel,
    ScheduledOperationModel,
)
from maais.db.models.platform import RunInstanceModel, ServiceInstanceModel


class ArtifactCatalogConflict(RuntimeError):
    pass


class ArtifactCatalogIntegrityError(RuntimeError):
    pass


class ArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start_attempt(
        self,
        attempt: ArtifactPublicationAttempt,
    ) -> ArtifactPublicationAttempt:
        if attempt.status is not PublicationAttemptStatus.STARTED:
            raise ValueError("new artifact publication attempt must be started")
        await self._operation_lock(attempt.operation_id)
        return await self._insert_attempt(attempt)

    async def begin_attempt(
        self,
        *,
        attempt_id: UUID,
        operation_id: UUID,
        bundle_content_hash: str,
        started_at: datetime,
    ) -> ArtifactPublicationAttempt:
        await self._operation_lock(operation_id)
        previous = await self._session.scalar(
            select(func.max(ArtifactPublicationAttemptModel.attempt)).where(
                ArtifactPublicationAttemptModel.operation_id == operation_id
            )
        )
        attempt = ArtifactPublicationAttempt.start(
            attempt_id=attempt_id,
            operation_id=operation_id,
            attempt=(previous or 0) + 1,
            bundle_content_hash=bundle_content_hash,
            started_at=started_at,
        )
        return await self._insert_attempt(attempt)

    async def _insert_attempt(
        self,
        attempt: ArtifactPublicationAttempt,
    ) -> ArtifactPublicationAttempt:
        existing = await self._session.get(ArtifactPublicationAttemptModel, attempt.id)
        if existing is not None:
            restored = _attempt_from_row(existing)
            if restored != attempt:
                raise ArtifactCatalogConflict("publication attempt identity is immutable")
            return restored
        operation = await self._session.get(ScheduledOperationModel, attempt.operation_id)
        if (
            operation is None
            or operation.status != "running"
            or attempt.started_at < operation.started_at
        ):
            raise ArtifactCatalogConflict("publication attempt operation is not active")
        previous = await self._session.scalar(
            select(func.max(ArtifactPublicationAttemptModel.attempt)).where(
                ArtifactPublicationAttemptModel.operation_id == attempt.operation_id
            )
        )
        expected = (previous or 0) + 1
        if attempt.attempt != expected:
            raise ArtifactCatalogConflict(
                f"publication attempt is not monotonic: expected={expected}"
            )
        created_id = await self._session.scalar(
            insert(ArtifactPublicationAttemptModel)
            .values(**_attempt_values(attempt))
            .on_conflict_do_nothing()
            .returning(ArtifactPublicationAttemptModel.id)
        )
        if created_id is None:
            raise ArtifactCatalogConflict("publication attempt conflicts with durable sequence")
        return attempt

    async def fail_attempt(
        self,
        attempt_id: UUID,
        *,
        reason_code: str,
        failed_at: datetime,
    ) -> ArtifactPublicationAttempt:
        row = await self._locked_attempt(attempt_id)
        current = _attempt_from_row(row)
        if current.status is PublicationAttemptStatus.FAILED:
            if current.reason_code == reason_code and current.completed_at == failed_at:
                return current
            raise ArtifactCatalogConflict("failed publication attempt evidence is immutable")
        if current.status is PublicationAttemptStatus.SUCCEEDED:
            raise ArtifactCatalogConflict("successful publication attempt cannot fail")
        updated = current.fail(reason_code=reason_code, failed_at=failed_at)
        _write_attempt(row, updated)
        return updated

    async def record_publication(self, record: ArtifactRecord) -> ArtifactRecord:
        await self._stream_lock(
            record.environment,
            record.candidate_hash,
            record.experiment_id,
        )
        existing = await self._session.scalar(
            select(ArtifactRecordModel)
            .where(
                ArtifactRecordModel.environment == record.environment,
                ArtifactRecordModel.candidate_hash == record.candidate_hash,
                ArtifactRecordModel.experiment_id == record.experiment_id,
                ArtifactRecordModel.artifact_type == record.artifact_type.value,
                ArtifactRecordModel.report_id == record.report_id,
            )
            .with_for_update()
        )
        if existing is not None:
            restored = _record_from_row(existing)
            if restored != record:
                raise ArtifactCatalogConflict("successful artifact record is immutable")
            return restored

        attempt_row = await self._locked_attempt(record.publication_attempt_id)
        attempt = _attempt_from_row(attempt_row)
        if (
            attempt.status is not PublicationAttemptStatus.STARTED
            or attempt.operation_id != record.operation_id
            or attempt.bundle_content_hash != record.bundle_content_hash
        ):
            raise ArtifactCatalogConflict(
                "artifact record does not match its active publication attempt"
            )
        operation = await self._session.get(ScheduledOperationModel, record.operation_id)
        run = await self._session.get(RunInstanceModel, record.run_id)
        producer = (
            await self._session.get(ServiceInstanceModel, operation.owner_boot_id)
            if operation is not None
            else None
        )
        if (
            operation is None
            or operation.status != "running"
            or operation.run_id != record.run_id
            or operation.experiment_id != record.experiment_id
            or operation.generated_at != record.generated_at
            or run is None
            or run.experiment_id != record.experiment_id
            or run.candidate_hash != record.candidate_hash
            or producer is None
            or producer.run_id != record.run_id
            or producer.candidate_hash != record.candidate_hash
            or producer.service_id != record.producing_service_id
            or producer.deployment_id != record.producing_deployment_id
            or producer.stopped_at is not None
        ):
            raise ArtifactCatalogConflict("artifact record authority identity is inconsistent")

        latest = await self._session.scalar(
            select(ArtifactRecordModel)
            .where(
                ArtifactRecordModel.environment == record.environment,
                ArtifactRecordModel.candidate_hash == record.candidate_hash,
                ArtifactRecordModel.experiment_id == record.experiment_id,
            )
            .order_by(ArtifactRecordModel.sequence.desc())
            .limit(1)
            .with_for_update()
        )
        expected_sequence = 1 if latest is None else latest.sequence + 1
        expected_previous = GENESIS_EVIDENCE_HASH if latest is None else latest.catalog_content_hash
        if record.sequence != expected_sequence:
            raise ArtifactCatalogConflict(
                f"artifact catalog sequence is not contiguous: expected={expected_sequence}"
            )
        if record.previous_evidence_hash != expected_previous:
            raise ArtifactCatalogConflict("artifact previous evidence hash is not the stream head")

        created_id = await self._session.scalar(
            insert(ArtifactRecordModel)
            .values(**_record_values(record))
            .on_conflict_do_nothing()
            .returning(ArtifactRecordModel.id)
        )
        if created_id is None:
            raise ArtifactCatalogConflict("artifact record conflicts with immutable catalog")
        _write_attempt(attempt_row, attempt.succeed(completed_at=record.recorded_at))
        return record

    async def list_stream(
        self,
        *,
        environment: str,
        candidate_hash: str,
        experiment_id: UUID,
    ) -> tuple[ArtifactRecord, ...]:
        rows = (
            await self._session.scalars(
                select(ArtifactRecordModel)
                .where(
                    ArtifactRecordModel.environment == environment,
                    ArtifactRecordModel.candidate_hash == candidate_hash,
                    ArtifactRecordModel.experiment_id == experiment_id,
                )
                .order_by(ArtifactRecordModel.sequence)
            )
        ).all()
        records: list[ArtifactRecord] = []
        previous = GENESIS_EVIDENCE_HASH
        for expected_sequence, row in enumerate(rows, start=1):
            try:
                record = _record_from_row(row)
            except (TypeError, ValueError) as error:
                raise ArtifactCatalogIntegrityError(
                    "artifact catalog row failed content validation"
                ) from error
            if record.sequence != expected_sequence or record.previous_evidence_hash != previous:
                raise ArtifactCatalogIntegrityError("artifact catalog chain is discontinuous")
            records.append(record)
            previous = record.catalog_content_hash
        return tuple(records)

    async def find_report(
        self,
        *,
        environment: str,
        candidate_hash: str,
        experiment_id: UUID,
        artifact_type: ArtifactType,
        report_id: str,
    ) -> ArtifactRecord | None:
        records = await self.list_stream(
            environment=environment,
            candidate_hash=candidate_hash,
            experiment_id=experiment_id,
        )
        for record in records:
            if record.artifact_type is artifact_type and record.report_id == report_id:
                return record
        return None

    async def get_record(self, record_id: UUID) -> ArtifactRecord:
        row = await self._session.get(ArtifactRecordModel, record_id)
        if row is None:
            raise LookupError("artifact catalog record does not exist")
        records = await self.list_stream(
            environment=row.environment,
            candidate_hash=row.candidate_hash,
            experiment_id=row.experiment_id,
        )
        for record in records:
            if record.id == record_id:
                return record
        raise ArtifactCatalogIntegrityError("artifact catalog record is absent from its stream")

    async def next_stream_position(
        self,
        *,
        environment: str,
        candidate_hash: str,
        experiment_id: UUID,
    ) -> tuple[int, str]:
        await self._stream_lock(environment, candidate_hash, experiment_id)
        records = await self.list_stream(
            environment=environment,
            candidate_hash=candidate_hash,
            experiment_id=experiment_id,
        )
        if not records:
            return 1, GENESIS_EVIDENCE_HASH
        latest = records[-1]
        return latest.sequence + 1, latest.catalog_content_hash

    async def _locked_attempt(self, attempt_id: UUID) -> ArtifactPublicationAttemptModel:
        row = await self._session.scalar(
            select(ArtifactPublicationAttemptModel)
            .where(ArtifactPublicationAttemptModel.id == attempt_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("artifact publication attempt does not exist")
        return row

    async def _operation_lock(self, operation_id: UUID) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"artifact-attempt:{operation_id}"},
        )

    async def _stream_lock(
        self,
        environment: str,
        candidate_hash: str,
        experiment_id: UUID,
    ) -> None:
        key = f"artifact-stream:{environment}:{candidate_hash}:{experiment_id}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )


def _attempt_values(attempt: ArtifactPublicationAttempt) -> dict[str, object]:
    return {
        "id": attempt.id,
        "operation_id": attempt.operation_id,
        "attempt": attempt.attempt,
        "bundle_content_hash": attempt.bundle_content_hash,
        "status": attempt.status.value,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "reason_code": attempt.reason_code,
        "content_hash": attempt.content_hash,
    }


def _attempt_from_row(row: ArtifactPublicationAttemptModel) -> ArtifactPublicationAttempt:
    try:
        return ArtifactPublicationAttempt(
            id=row.id,
            operation_id=row.operation_id,
            attempt=row.attempt,
            bundle_content_hash=row.bundle_content_hash,
            status=PublicationAttemptStatus(row.status),
            started_at=row.started_at,
            completed_at=row.completed_at,
            reason_code=row.reason_code,
            content_hash=row.content_hash,
        )
    except ValueError as error:
        raise ArtifactCatalogIntegrityError(
            "stored artifact publication attempt is invalid"
        ) from error


def _write_attempt(
    row: ArtifactPublicationAttemptModel,
    attempt: ArtifactPublicationAttempt,
) -> None:
    row.status = attempt.status.value
    row.completed_at = attempt.completed_at
    row.reason_code = attempt.reason_code
    row.content_hash = attempt.content_hash


def _record_values(record: ArtifactRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "operation_id": record.operation_id,
        "publication_attempt_id": record.publication_attempt_id,
        "environment": record.environment,
        "candidate_hash": record.candidate_hash,
        "experiment_id": record.experiment_id,
        "run_id": record.run_id,
        "artifact_type": record.artifact_type.value,
        "report_id": record.report_id,
        "bundle_content_hash": record.bundle_content_hash,
        "size_bytes": record.size_bytes,
        "media_type": record.media_type,
        "generated_at": record.generated_at,
        "recorded_at": record.recorded_at,
        "producing_deployment_id": record.producing_deployment_id,
        "producing_service_id": record.producing_service_id,
        "sequence": record.sequence,
        "replica_inventory": [stored_artifact_json(value) for value in record.replica_inventory],
        "canonical_inventory": [
            stored_artifact_json(value) for value in record.canonical_inventory
        ],
        "previous_evidence_hash": record.previous_evidence_hash,
        "catalog_content_hash": record.catalog_content_hash,
    }


def _record_from_row(row: ArtifactRecordModel) -> ArtifactRecord:
    try:
        return ArtifactRecord(
            id=row.id,
            operation_id=row.operation_id,
            publication_attempt_id=row.publication_attempt_id,
            environment=row.environment,
            candidate_hash=row.candidate_hash,
            experiment_id=row.experiment_id,
            run_id=row.run_id,
            artifact_type=ArtifactType(row.artifact_type),
            report_id=row.report_id,
            bundle_content_hash=row.bundle_content_hash,
            size_bytes=row.size_bytes,
            media_type=row.media_type,
            generated_at=row.generated_at,
            recorded_at=row.recorded_at,
            producing_deployment_id=row.producing_deployment_id,
            producing_service_id=row.producing_service_id,
            sequence=row.sequence,
            replica_inventory=tuple(
                stored_artifact_from_json(value) for value in row.replica_inventory
            ),
            canonical_inventory=tuple(
                stored_artifact_from_json(value) for value in row.canonical_inventory
            ),
            previous_evidence_hash=row.previous_evidence_hash,
            catalog_content_hash=row.catalog_content_hash,
        )
    except (TypeError, ValueError) as error:
        raise ArtifactCatalogIntegrityError("stored artifact catalog record is invalid") from error
