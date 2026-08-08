"""Exactly-once scheduled operation ownership and takeover authority."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.artifacts.models import (
    ScheduledOperation,
    ScheduledOperationStatus,
    ScheduledOperationType,
)
from maais.config.cloud import ServiceRole
from maais.db.models.artifacts import ScheduledOperationModel
from maais.db.models.platform import RunInstanceModel, ServiceInstanceModel


class ScheduledOperationConflict(RuntimeError):
    pass


class ScheduledOperationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def acquire(self, candidate: ScheduledOperation) -> ScheduledOperation:
        if candidate.status is not ScheduledOperationStatus.RUNNING or candidate.attempt != 1:
            raise ValueError("operation acquisition requires a new running candidate")
        await self._advisory_lock(candidate)
        await self._require_eligible_owner(candidate)
        created_id = await self._session.scalar(
            insert(ScheduledOperationModel)
            .values(**_operation_values(candidate))
            .on_conflict_do_nothing(
                index_elements=[
                    ScheduledOperationModel.run_id,
                    ScheduledOperationModel.operation_type,
                    ScheduledOperationModel.berlin_date,
                ]
            )
            .returning(ScheduledOperationModel.id)
        )
        if created_id is not None:
            return candidate

        row = await self._session.scalar(
            select(ScheduledOperationModel)
            .where(
                ScheduledOperationModel.run_id == candidate.run_id,
                ScheduledOperationModel.operation_type == candidate.operation_type.value,
                ScheduledOperationModel.berlin_date == candidate.berlin_date,
            )
            .with_for_update()
        )
        if row is None:
            raise ScheduledOperationConflict("scheduled operation disappeared after conflict")
        current = _operation_from_row(row)
        if current.experiment_id != candidate.experiment_id:
            raise ScheduledOperationConflict("operation key belongs to another experiment")
        if current.status is ScheduledOperationStatus.SUCCEEDED:
            return current
        if (
            current.status is ScheduledOperationStatus.RUNNING
            and current.owner_boot_id == candidate.owner_boot_id
        ):
            return current
        if current.owner_boot_id != candidate.owner_boot_id:
            previous_owner = await self._session.scalar(
                select(ServiceInstanceModel)
                .where(ServiceInstanceModel.boot_id == current.owner_boot_id)
                .with_for_update()
            )
            if previous_owner is None or previous_owner.stopped_at is None:
                raise ScheduledOperationConflict(
                    "scheduled operation has an active owner and cannot be taken over"
                )
            if candidate.started_at < previous_owner.stopped_at:
                raise ScheduledOperationConflict(
                    "operation takeover cannot precede the previous owner's terminal time"
                )
        updated = current.takeover(
            owner_boot_id=candidate.owner_boot_id,
            started_at=candidate.started_at,
        )
        _write_operation(row, updated)
        return updated

    async def fail(
        self,
        operation_id: UUID,
        *,
        owner_boot_id: UUID,
        reason_code: str,
        failed_at: datetime,
    ) -> ScheduledOperation:
        row = await self._locked(operation_id)
        current = _operation_from_row(row)
        if current.owner_boot_id != owner_boot_id:
            raise ScheduledOperationConflict("only the current owner can fail an operation")
        if current.status is ScheduledOperationStatus.FAILED:
            if current.reason_code == reason_code and current.completed_at == failed_at:
                return current
            raise ScheduledOperationConflict("failed operation terminal evidence is immutable")
        updated = current.fail(reason_code=reason_code, failed_at=failed_at)
        _write_operation(row, updated)
        return updated

    async def complete(
        self,
        operation_id: UUID,
        *,
        owner_boot_id: UUID,
        result_artifact_ids: tuple[UUID, ...],
        completed_at: datetime,
    ) -> ScheduledOperation:
        row = await self._locked(operation_id)
        current = _operation_from_row(row)
        if current.owner_boot_id != owner_boot_id:
            raise ScheduledOperationConflict("only the current owner can complete an operation")
        if current.status is ScheduledOperationStatus.SUCCEEDED:
            if (
                current.result_artifact_ids == result_artifact_ids
                and current.completed_at == completed_at
            ):
                return current
            raise ScheduledOperationConflict("successful operation evidence is immutable")
        updated = current.succeed(
            result_artifact_ids=result_artifact_ids,
            completed_at=completed_at,
        )
        _write_operation(row, updated)
        return updated

    async def get(self, operation_id: UUID) -> ScheduledOperation:
        row = await self._session.get(ScheduledOperationModel, operation_id)
        if row is None:
            raise LookupError("scheduled operation does not exist")
        return _operation_from_row(row)

    async def _locked(self, operation_id: UUID) -> ScheduledOperationModel:
        row = await self._session.scalar(
            select(ScheduledOperationModel)
            .where(ScheduledOperationModel.id == operation_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("scheduled operation does not exist")
        return row

    async def _require_eligible_owner(self, operation: ScheduledOperation) -> None:
        owner = await self._session.scalar(
            select(ServiceInstanceModel)
            .where(ServiceInstanceModel.boot_id == operation.owner_boot_id)
            .with_for_update()
        )
        run = await self._session.get(RunInstanceModel, operation.run_id)
        if (
            owner is None
            or owner.run_id != operation.run_id
            or owner.service_role != ServiceRole.OPERATIONS.value
            or owner.stopped_at is not None
            or operation.started_at < owner.first_seen_at
            or run is None
            or run.experiment_id != operation.experiment_id
        ):
            raise ScheduledOperationConflict(
                "operation owner must be an active operations service for the run"
            )

    async def _advisory_lock(self, operation: ScheduledOperation) -> None:
        key = (
            f"scheduled-operation:{operation.run_id}:"
            f"{operation.operation_type.value}:{operation.berlin_date.isoformat()}"
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": key},
        )


def _operation_values(operation: ScheduledOperation) -> dict[str, object]:
    return {
        "id": operation.id,
        "run_id": operation.run_id,
        "experiment_id": operation.experiment_id,
        "operation_type": operation.operation_type.value,
        "berlin_date": operation.berlin_date,
        "status": operation.status.value,
        "owner_boot_id": operation.owner_boot_id,
        "generated_at": operation.generated_at,
        "attempt": operation.attempt,
        "result_artifact_ids": [str(value) for value in operation.result_artifact_ids],
        "reason_code": operation.reason_code,
        "started_at": operation.started_at,
        "completed_at": operation.completed_at,
        "content_hash": operation.content_hash,
    }


def _operation_from_row(row: ScheduledOperationModel) -> ScheduledOperation:
    try:
        result_ids = tuple(UUID(str(value)) for value in row.result_artifact_ids)
        return ScheduledOperation(
            id=row.id,
            run_id=row.run_id,
            experiment_id=row.experiment_id,
            operation_type=ScheduledOperationType(row.operation_type),
            berlin_date=row.berlin_date,
            status=ScheduledOperationStatus(row.status),
            owner_boot_id=row.owner_boot_id,
            generated_at=row.generated_at,
            attempt=row.attempt,
            result_artifact_ids=result_ids,
            reason_code=row.reason_code,
            started_at=row.started_at,
            completed_at=row.completed_at,
            content_hash=row.content_hash,
        )
    except (TypeError, ValueError) as error:
        raise ScheduledOperationConflict(
            "stored scheduled operation evidence is invalid"
        ) from error


def _write_operation(row: ScheduledOperationModel, operation: ScheduledOperation) -> None:
    values = _operation_values(operation)
    for name, value in values.items():
        if name not in {"id", "run_id", "experiment_id", "operation_type", "berlin_date"}:
            setattr(row, name, value)
