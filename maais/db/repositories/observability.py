"""Serialized append-only persistence for audit and health evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.observability import AuditEventModel, HealthEvaluationModel
from maais.db.models.platform import ServiceInstanceModel
from maais.domain.json import JsonValue, MutableJsonValue, freeze_json, to_json_data
from maais.observability.audit import (
    AuditChainVerification,
    AuditEvent,
    AuditSourceRole,
    HealthEvaluation,
    HealthSeverity,
    HealthStatus,
    verify_audit_chain,
)

_AUDIT_LOCK_KEY = "maais:audit-chain:v1"
_RUNTIME_SOURCE_BY_DATABASE_ROLE = {
    "maais_migrator": AuditSourceRole.MIGRATOR,
    "maais_ops": AuditSourceRole.OPERATIONS,
    "maais_web": AuditSourceRole.WEB,
    "maais_worker": AuditSourceRole.WORKER,
}


class ObservabilityIntegrityError(RuntimeError):
    pass


class ObservabilityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_audit(
        self,
        *,
        event_id: UUID,
        source_role: AuditSourceRole,
        actor_reference: str,
        session_reference: str | None,
        event_code: str,
        reason_code: str | None,
        evidence: Mapping[str, object],
        run_id: UUID | None,
        service_boot_id: UUID | None,
        occurred_at: datetime,
    ) -> AuditEvent:
        database_role = str(await self._session.scalar(text("SELECT current_user")))
        runtime_source = _RUNTIME_SOURCE_BY_DATABASE_ROLE.get(database_role)
        if runtime_source is not None:
            if runtime_source is not source_role:
                raise ObservabilityIntegrityError(
                    "database role cannot append an audit event for another source role"
                )
            sequence = await self._append_audit_via_gateway(
                event_id=event_id,
                actor_reference=actor_reference,
                session_reference=session_reference,
                event_code=event_code,
                reason_code=reason_code,
                evidence=evidence,
                run_id=run_id,
                service_boot_id=service_boot_id,
                occurred_at=occurred_at,
            )
            return await self.get_audit(sequence)
        return await self._append_audit_direct(
            event_id=event_id,
            source_role=source_role,
            actor_reference=actor_reference,
            session_reference=session_reference,
            event_code=event_code,
            reason_code=reason_code,
            evidence=evidence,
            run_id=run_id,
            service_boot_id=service_boot_id,
            occurred_at=occurred_at,
        )

    async def get_audit(self, sequence: int) -> AuditEvent:
        row = await self._session.get(AuditEventModel, sequence)
        if row is None:
            raise LookupError("audit event does not exist")
        return _audit_from_row(row)

    async def list_audit_events(self) -> tuple[AuditEvent, ...]:
        rows = tuple(
            await self._session.scalars(select(AuditEventModel).order_by(AuditEventModel.sequence))
        )
        return tuple(_audit_from_row(row) for row in rows)

    async def verify_audit_chain(self) -> AuditChainVerification:
        return verify_audit_chain(await self.list_audit_events())

    async def record_health(self, evaluation: HealthEvaluation) -> HealthEvaluation:
        await self._require_operations_service(evaluation)
        if evaluation.recovery_of_evaluation_id is not None:
            prior = await self._session.get(
                HealthEvaluationModel,
                evaluation.recovery_of_evaluation_id,
            )
            if (
                prior is None
                or prior.run_id != evaluation.run_id
                or prior.checked_at >= evaluation.checked_at
                or prior.overall_status == HealthStatus.HEALTHY.value
            ):
                raise ObservabilityIntegrityError(
                    "health recovery must reference an earlier unhealthy evaluation for the run"
                )
        created = await self._session.scalar(
            insert(HealthEvaluationModel)
            .values(**_health_values(evaluation))
            .on_conflict_do_nothing()
            .returning(HealthEvaluationModel.evaluation_id)
        )
        if created is not None:
            return evaluation
        row = await self._session.scalar(
            select(HealthEvaluationModel)
            .where(HealthEvaluationModel.evaluation_id == evaluation.evaluation_id)
            .with_for_update()
        )
        if row is None:
            raise ObservabilityIntegrityError(
                "health run and checked_at identity belongs to another evaluation"
            )
        restored = _health_from_row(row)
        if restored != evaluation:
            raise ObservabilityIntegrityError("immutable health evaluation identity has changed")
        return restored

    async def get_health(self, evaluation_id: UUID) -> HealthEvaluation:
        row = await self._session.get(HealthEvaluationModel, evaluation_id)
        if row is None:
            raise LookupError("health evaluation does not exist")
        return _health_from_row(row)

    async def latest_health(self, run_id: UUID) -> HealthEvaluation | None:
        row = await self._session.scalar(
            select(HealthEvaluationModel)
            .where(HealthEvaluationModel.run_id == run_id)
            .order_by(
                HealthEvaluationModel.checked_at.desc(),
                HealthEvaluationModel.evaluation_id.desc(),
            )
            .limit(1)
        )
        return _health_from_row(row) if row is not None else None

    async def _append_audit_direct(
        self,
        *,
        event_id: UUID,
        source_role: AuditSourceRole,
        actor_reference: str,
        session_reference: str | None,
        event_code: str,
        reason_code: str | None,
        evidence: Mapping[str, object],
        run_id: UUID | None,
        service_boot_id: UUID | None,
        occurred_at: datetime,
    ) -> AuditEvent:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 22002))"),
            {"key": _AUDIT_LOCK_KEY},
        )
        existing = await self._session.scalar(
            select(AuditEventModel).where(AuditEventModel.event_id == event_id).with_for_update()
        )
        if existing is not None:
            restored = _audit_from_row(existing)
            candidate = AuditEvent.create(
                event_id=event_id,
                sequence=restored.sequence,
                previous_hash=restored.previous_hash,
                source_role=source_role,
                actor_reference=actor_reference,
                session_reference=session_reference,
                event_code=event_code,
                reason_code=reason_code,
                evidence=evidence,
                run_id=run_id,
                service_boot_id=service_boot_id,
                occurred_at=occurred_at,
            )
            if candidate != restored:
                raise ObservabilityIntegrityError("immutable audit identity has changed")
            return restored
        latest = await self._session.scalar(
            select(AuditEventModel).order_by(AuditEventModel.sequence.desc()).limit(1)
        )
        event = AuditEvent.create(
            event_id=event_id,
            sequence=(latest.sequence + 1 if latest is not None else 1),
            previous_hash=(latest.content_hash if latest is not None else None),
            source_role=source_role,
            actor_reference=actor_reference,
            session_reference=session_reference,
            event_code=event_code,
            reason_code=reason_code,
            evidence=evidence,
            run_id=run_id,
            service_boot_id=service_boot_id,
            occurred_at=occurred_at,
        )
        self._session.add(AuditEventModel(**_audit_values(event)))
        await self._session.flush()
        return event

    async def _append_audit_via_gateway(
        self,
        *,
        event_id: UUID,
        actor_reference: str,
        session_reference: str | None,
        event_code: str,
        reason_code: str | None,
        evidence: Mapping[str, object],
        run_id: UUID | None,
        service_boot_id: UUID | None,
        occurred_at: datetime,
    ) -> int:
        normalized = to_json_data(evidence)
        if not isinstance(normalized, dict):  # pragma: no cover - Mapping input invariant
            raise TypeError("audit evidence must be an object")
        sequence = await self._session.scalar(
            text(
                "SELECT public.maais_append_audit_event("
                ":event_id, :actor_reference, :session_reference, :event_code, "
                ":reason_code, CAST(:evidence AS jsonb), :run_id, :service_boot_id, "
                ":occurred_at)"
            ),
            {
                "event_id": event_id,
                "actor_reference": actor_reference,
                "session_reference": session_reference,
                "event_code": event_code,
                "reason_code": reason_code,
                "evidence": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                "run_id": run_id,
                "service_boot_id": service_boot_id,
                "occurred_at": occurred_at,
            },
        )
        if type(sequence) is not int:
            raise ObservabilityIntegrityError("audit gateway returned an invalid sequence")
        return sequence

    async def _require_operations_service(self, evaluation: HealthEvaluation) -> None:
        service = await self._session.scalar(
            select(ServiceInstanceModel)
            .where(ServiceInstanceModel.boot_id == evaluation.service_boot_id)
            .with_for_update()
        )
        if (
            service is None
            or service.run_id != evaluation.run_id
            or service.service_role != "operations"
            or service.stopped_at is not None
            or evaluation.checked_at < service.first_seen_at
        ):
            raise ObservabilityIntegrityError(
                "health evaluation requires an active operations service for the run"
            )


def _audit_values(event: AuditEvent) -> dict[str, object]:
    evidence = to_json_data(event.evidence)
    assert isinstance(evidence, dict)
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "previous_hash": event.previous_hash,
        "source_role": event.source_role.value,
        "actor_reference": event.actor_reference,
        "session_reference": event.session_reference,
        "event_code": event.event_code,
        "reason_code": event.reason_code,
        "evidence_json": evidence,
        "run_id": event.run_id,
        "service_boot_id": event.service_boot_id,
        "occurred_at": event.occurred_at,
        "content_hash": event.content_hash,
    }


def _audit_from_row(row: AuditEventModel) -> AuditEvent:
    try:
        return AuditEvent(
            event_id=row.event_id,
            sequence=row.sequence,
            previous_hash=row.previous_hash,
            source_role=AuditSourceRole(row.source_role),
            actor_reference=row.actor_reference,
            session_reference=row.session_reference,
            event_code=row.event_code,
            reason_code=row.reason_code,
            evidence=_json_mapping(row.evidence_json),
            run_id=row.run_id,
            service_boot_id=row.service_boot_id,
            occurred_at=row.occurred_at,
            content_hash=row.content_hash,
        )
    except (TypeError, ValueError) as error:
        raise ObservabilityIntegrityError("stored audit event is invalid") from error


def _health_values(evaluation: HealthEvaluation) -> dict[str, object]:
    components = to_json_data(evaluation.components)
    assert isinstance(components, dict)
    return {
        "evaluation_id": evaluation.evaluation_id,
        "run_id": evaluation.run_id,
        "service_boot_id": evaluation.service_boot_id,
        "overall_status": evaluation.overall_status.value,
        "failed_check_names": list(evaluation.failed_check_names),
        "severity": evaluation.severity.value,
        "deduplication_key": evaluation.deduplication_key,
        "incident_id": evaluation.incident_id,
        "recovery_of_evaluation_id": evaluation.recovery_of_evaluation_id,
        "recovered_at": evaluation.recovered_at,
        "component_json": components,
        "checked_at": evaluation.checked_at,
        "content_hash": evaluation.content_hash,
    }


def _health_from_row(row: HealthEvaluationModel) -> HealthEvaluation:
    try:
        failed = tuple(str(value) for value in row.failed_check_names)
        return HealthEvaluation(
            evaluation_id=row.evaluation_id,
            run_id=row.run_id,
            service_boot_id=row.service_boot_id,
            overall_status=HealthStatus(row.overall_status),
            failed_check_names=failed,
            severity=HealthSeverity(row.severity),
            deduplication_key=row.deduplication_key,
            incident_id=row.incident_id,
            recovery_of_evaluation_id=row.recovery_of_evaluation_id,
            recovered_at=row.recovered_at,
            components=_json_mapping(row.component_json),
            checked_at=row.checked_at,
            content_hash=row.content_hash,
        )
    except (TypeError, ValueError) as error:
        raise ObservabilityIntegrityError("stored health evaluation is invalid") from error


def _json_mapping(value: Mapping[str, MutableJsonValue]) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):  # pragma: no cover - model type invariant
        raise TypeError("stored operational JSON must be an object")
    return frozen
