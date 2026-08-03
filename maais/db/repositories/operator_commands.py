"""Transactional, event-backed operator command inbox."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import OperatorCommandModel
from maais.db.repositories.events import EventRepository
from maais.domain.events import NewDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, freeze_json, to_json_data
from maais.operations.operator_commands import CommandStatus, CommandType, OperatorCommand


class OperatorCommandConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OperatorCommandWriteResult:
    created: bool
    command: OperatorCommand


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):
        raise TypeError("operator command state must be a JSON object")
    return normalized


def _event_object(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("operator command event must be a JSON object")
    return normalized


def _new_event(command: OperatorCommand, event_type: str, occurred_at: datetime) -> NewDomainEvent:
    return NewDomainEvent(
        aggregate_id=command.command_id,
        aggregate_type="operator_command",
        event_type=event_type,
        payload=_event_object(command.to_dict()),
        metadata={"schema_revision": "0017"},
        occurred_at=occurred_at,
    )


def _from_row(row: OperatorCommandModel) -> OperatorCommand:
    return OperatorCommand(
        command_id=row.id,
        experiment_id=row.experiment_id,
        command_type=CommandType(row.command_type),
        status=CommandStatus(row.status),
        idempotency_key=row.idempotency_key,
        actor=row.actor,
        reason=row.reason,
        payload=_event_object(row.payload_json),
        operator_confirmed=row.operator_confirmed,
        request_hash=row.request_hash,
        requested_at=row.requested_at,
        version=row.version,
        accepted_at=row.accepted_at,
        accepted_by=row.accepted_by,
        completed_at=row.completed_at,
        result=(_event_object(row.result_json) if row.result_json is not None else None),
    )


def _verify(row: OperatorCommandModel) -> OperatorCommand:
    command = _from_row(row)
    if command.content_hash != row.content_hash:
        raise OperatorCommandConflict("operator command content hash is invalid")
    return command


class OperatorCommandRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def enqueue(self, command: OperatorCommand) -> OperatorCommandWriteResult:
        if command.status is not CommandStatus.REQUESTED:
            raise ValueError("only a requested operator command can enter the inbox")
        created_id = await self._session.scalar(
            insert(OperatorCommandModel)
            .values(
                id=command.command_id,
                experiment_id=command.experiment_id,
                command_type=command.command_type.value,
                status=command.status.value,
                idempotency_key=command.idempotency_key,
                actor=command.actor,
                reason=command.reason,
                payload_json=_json_object(command.payload),
                operator_confirmed=command.operator_confirmed,
                request_hash=command.request_hash,
                requested_at=command.requested_at,
                version=command.version,
                accepted_at=None,
                accepted_by=None,
                completed_at=None,
                result_json=None,
                content_hash=command.content_hash,
            )
            .on_conflict_do_nothing()
            .returning(OperatorCommandModel.id)
        )
        if created_id is None:
            existing = await self._session.scalar(
                select(OperatorCommandModel)
                .where(
                    OperatorCommandModel.experiment_id == command.experiment_id,
                    OperatorCommandModel.idempotency_key == command.idempotency_key,
                )
                .with_for_update()
            )
            if existing is None:
                raise OperatorCommandConflict("operator command identity already exists")
            restored = _verify(existing)
            if restored.request_hash != command.request_hash:
                raise OperatorCommandConflict(
                    "idempotency key was already used for a different operator request"
                )
            return OperatorCommandWriteResult(created=False, command=restored)

        await self._events.append(
            command.command_id,
            "operator_command",
            0,
            (_new_event(command, "operator_command.requested", command.requested_at),),
        )
        return OperatorCommandWriteResult(created=True, command=command)

    async def get(self, command_id: UUID) -> OperatorCommand:
        row = await self._session.get(OperatorCommandModel, command_id)
        if row is None:
            raise LookupError("operator command does not exist")
        return _verify(row)

    async def list_for_experiment(
        self,
        experiment_id: UUID,
        *,
        status: CommandStatus | None = None,
        limit: int = 100,
    ) -> tuple[OperatorCommand, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("operator command limit must be between 1 and 500")
        statement = select(OperatorCommandModel).where(
            OperatorCommandModel.experiment_id == experiment_id
        )
        if status is not None:
            statement = statement.where(OperatorCommandModel.status == status.value)
        rows = (
            await self._session.scalars(
                statement.order_by(
                    OperatorCommandModel.requested_at.desc(),
                    OperatorCommandModel.id.desc(),
                ).limit(limit)
            )
        ).all()
        return tuple(_verify(row) for row in rows)

    async def claim_next(
        self,
        experiment_id: UUID,
        *,
        worker_id: str,
        accepted_at: datetime,
    ) -> OperatorCommand | None:
        row = await self._session.scalar(
            select(OperatorCommandModel)
            .where(
                OperatorCommandModel.experiment_id == experiment_id,
                OperatorCommandModel.status.in_(
                    (
                        CommandStatus.ACCEPTED.value,
                        CommandStatus.REQUESTED.value,
                    )
                ),
            )
            .order_by(
                case(
                    (OperatorCommandModel.status == CommandStatus.ACCEPTED.value, 0),
                    else_=1,
                ),
                OperatorCommandModel.requested_at,
                OperatorCommandModel.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        current = _verify(row)
        if current.status is CommandStatus.ACCEPTED:
            recovered = current.take_over(worker_id=worker_id)
            if recovered == current:
                return current
            row.version = recovered.version
            row.accepted_by = recovered.accepted_by
            row.content_hash = recovered.content_hash
            await self._events.append(
                recovered.command_id,
                "operator_command",
                current.version,
                (
                    _new_event(
                        recovered,
                        "operator_command.recovered",
                        accepted_at,
                    ),
                ),
            )
            return recovered
        accepted = current.accept(accepted_at=accepted_at, worker_id=worker_id)
        row.status = accepted.status.value
        row.version = accepted.version
        row.accepted_at = accepted.accepted_at
        row.accepted_by = accepted.accepted_by
        row.content_hash = accepted.content_hash
        await self._events.append(
            accepted.command_id,
            "operator_command",
            current.version,
            (_new_event(accepted, "operator_command.accepted", accepted_at),),
        )
        return accepted

    async def complete(
        self,
        command_id: UUID,
        *,
        worker_id: str,
        completed_at: datetime,
        result: Mapping[str, object],
    ) -> OperatorCommand:
        row = await self._locked(command_id)
        current = _verify(row)
        if current.accepted_by != worker_id:
            raise OperatorCommandConflict("operator command belongs to a different worker")
        completed = current.complete(completed_at=completed_at, result=result)
        if completed == current:
            return current
        row.status = completed.status.value
        row.version = completed.version
        row.completed_at = completed.completed_at
        row.result_json = _json_object(completed.result)
        row.content_hash = completed.content_hash
        await self._events.append(
            completed.command_id,
            "operator_command",
            current.version,
            (_new_event(completed, "operator_command.completed", completed_at),),
        )
        return completed

    async def reject(
        self,
        command_id: UUID,
        *,
        worker_id: str,
        completed_at: datetime,
        reason_code: str,
        detail: str,
    ) -> OperatorCommand:
        row = await self._locked(command_id)
        current = _verify(row)
        if current.accepted_by != worker_id:
            raise OperatorCommandConflict("operator command belongs to a different worker")
        rejected = current.reject(
            completed_at=completed_at,
            reason_code=reason_code,
            detail=detail,
        )
        if rejected == current:
            return current
        row.status = rejected.status.value
        row.version = rejected.version
        row.completed_at = rejected.completed_at
        row.result_json = _json_object(rejected.result)
        row.content_hash = rejected.content_hash
        await self._events.append(
            rejected.command_id,
            "operator_command",
            current.version,
            (_new_event(rejected, "operator_command.rejected", completed_at),),
        )
        return rejected

    async def _locked(self, command_id: UUID) -> OperatorCommandModel:
        row = await self._session.scalar(
            select(OperatorCommandModel)
            .where(OperatorCommandModel.id == command_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("operator command does not exist")
        return row
