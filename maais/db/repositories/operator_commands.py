"""Transactional, event-backed operator command inbox."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import OperatorCommandModel
from maais.db.models.platform import RunInstanceModel, ServiceInstanceModel
from maais.db.repositories.events import EventRepository
from maais.db.repositories.observability import ObservabilityRepository
from maais.domain.events import NewDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, freeze_json, to_json_data
from maais.observability.audit import (
    AuditSourceRole,
    bounded_reason_code,
    deterministic_audit_event_id,
    pseudonymous_reference,
)
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
    def __init__(
        self,
        session: AsyncSession,
        events: EventRepository,
        observability: ObservabilityRepository,
    ) -> None:
        self._session = session
        self._events = events
        self._observability = observability

    async def enqueue(self, command: OperatorCommand) -> OperatorCommandWriteResult:
        if command.status is not CommandStatus.REQUESTED:
            raise ValueError("only a requested operator command can enter the inbox")
        if str(await self._session.scalar(text("SELECT current_user"))) == "maais_web":
            return await self._enqueue_via_gateway(command)
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

    async def _enqueue_via_gateway(
        self,
        command: OperatorCommand,
    ) -> OperatorCommandWriteResult:
        await self._session.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock("
                "pg_catalog.hashtextextended(CAST(:identity AS text), 19017))"
            ),
            {"identity": f"{command.experiment_id}:{command.idempotency_key}"},
        )
        existing = await self._session.scalar(
            select(OperatorCommandModel).where(
                OperatorCommandModel.experiment_id == command.experiment_id,
                OperatorCommandModel.idempotency_key == command.idempotency_key,
            )
        )
        if existing is not None:
            restored = _verify(existing)
            if restored.request_hash != command.request_hash:
                raise OperatorCommandConflict(
                    "idempotency key was already used for a different operator request"
                )
            return OperatorCommandWriteResult(created=False, command=restored)

        payload = to_json_data(command.payload)
        if not isinstance(payload, dict):  # pragma: no cover - command domain invariant
            raise TypeError("operator command payload must be an object")
        try:
            created_id = await self._session.scalar(
                text(
                    "SELECT public.maais_enqueue_operator_command("
                    ":command_id, :experiment_id, :command_type, :idempotency_key, "
                    ":actor, :reason, CAST(:payload AS jsonb), :confirmed, :requested_at)"
                ),
                {
                    "command_id": command.command_id,
                    "experiment_id": command.experiment_id,
                    "command_type": command.command_type.value,
                    "idempotency_key": command.idempotency_key,
                    "actor": command.actor,
                    "reason": command.reason,
                    "payload": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    "confirmed": command.operator_confirmed,
                    "requested_at": command.requested_at,
                },
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise OperatorCommandConflict("operator command identity already exists") from error
            raise
        if created_id != command.command_id:
            raise OperatorCommandConflict("operator command gateway returned another identity")
        row = await self._session.get(OperatorCommandModel, command.command_id)
        if row is None:
            raise OperatorCommandConflict("operator command gateway did not persist the request")
        restored = _verify(row)
        if restored.request_hash != command.request_hash:
            raise OperatorCommandConflict(
                "idempotency key was already used for a different operator request"
            )
        return OperatorCommandWriteResult(created=True, command=restored)

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
        await self._audit_command(
            accepted,
            worker_id=worker_id,
            event_code="operator.command.accepted",
            reason_code="worker_claimed",
            occurred_at=accepted_at,
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
            assert current.completed_at is not None
            await self._audit_command(
                current,
                worker_id=worker_id,
                event_code="operator.command.completed",
                reason_code="command_completed",
                occurred_at=current.completed_at,
            )
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
        await self._audit_command(
            completed,
            worker_id=worker_id,
            event_code="operator.command.completed",
            reason_code="command_completed",
            occurred_at=completed_at,
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
            assert current.completed_at is not None
            stored_reason = (
                str(current.result.get("reason_code"))
                if current.result is not None
                else "command_rejected"
            )
            await self._audit_command(
                current,
                worker_id=worker_id,
                event_code="operator.command.rejected",
                reason_code=bounded_reason_code(
                    stored_reason,
                    fallback="command_rejected",
                ),
                occurred_at=current.completed_at,
            )
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
        await self._audit_command(
            rejected,
            worker_id=worker_id,
            event_code="operator.command.rejected",
            reason_code=bounded_reason_code(reason_code, fallback="command_rejected"),
            occurred_at=completed_at,
        )
        return rejected

    async def _audit_command(
        self,
        command: OperatorCommand,
        *,
        worker_id: str,
        event_code: str,
        reason_code: str,
        occurred_at: datetime,
    ) -> None:
        run_id: UUID | None = None
        raw_run_id = command.payload.get("run_id")
        if isinstance(raw_run_id, str):
            try:
                candidate_run_id = UUID(raw_run_id)
            except ValueError:
                candidate_run_id = None
            if candidate_run_id is not None:
                run = await self._session.get(RunInstanceModel, candidate_run_id)
                if run is not None and run.experiment_id == command.experiment_id:
                    run_id = candidate_run_id

        service_boot_id: UUID | None = None
        prefix = "paper_worker:"
        if worker_id.startswith(prefix):
            try:
                candidate_boot_id = UUID(worker_id[len(prefix) :])
            except ValueError:
                candidate_boot_id = None
            if candidate_boot_id is not None:
                service = await self._session.get(ServiceInstanceModel, candidate_boot_id)
                if (
                    service is not None
                    and service.service_role == "worker"
                    and (run_id is None or service.run_id == run_id)
                ):
                    service_boot_id = candidate_boot_id

        await self._observability.append_audit(
            event_id=deterministic_audit_event_id(
                event_code,
                f"{command.command_id}:{command.version}",
            ),
            source_role=AuditSourceRole.WORKER,
            actor_reference=pseudonymous_reference("service", worker_id),
            session_reference=None,
            event_code=event_code,
            reason_code=reason_code,
            evidence={
                "command_id": str(command.command_id),
                "command_type": command.command_type.value,
                "experiment_id": str(command.experiment_id),
                "status": command.status.value,
                "version": command.version,
            },
            run_id=run_id,
            service_boot_id=service_boot_id,
            occurred_at=occurred_at,
        )

    async def _locked(self, command_id: UUID) -> OperatorCommandModel:
        row = await self._session.scalar(
            select(OperatorCommandModel)
            .where(OperatorCommandModel.id == command_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("operator command does not exist")
        return row
