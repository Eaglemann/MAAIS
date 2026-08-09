from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from maais.config.cloud import ServiceRole
from maais.db.models.observability import AuditEventModel, HealthEvaluationModel
from maais.db.unit_of_work import UnitOfWork
from maais.observability.audit import (
    AuditSourceRole,
    HealthEvaluation,
    HealthSeverity,
    HealthStatus,
    health_deduplication_key,
    pseudonymous_reference,
)
from tests.integration.test_platform_repository import (
    EXPERIMENT_ONE,
    RUN_ONE,
    _prepare_run,
    _service,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 9, 1, tzinfo=timezone.utc)
EVENT_ONE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EVENT_TWO = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
EVALUATION_ONE = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
EVALUATION_TWO = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
OPERATIONS_BOOT = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


async def test_concurrent_audit_appends_are_gapless_and_hash_chained(
    uow_factory: UnitOfWork,
) -> None:
    async def append(event_id: UUID, occurred_at: datetime) -> None:
        async with uow_factory.begin() as uow:
            await uow.observability.append_audit(
                event_id=event_id,
                source_role=AuditSourceRole.WEB,
                actor_reference=pseudonymous_reference("actor", "sole_operator"),
                session_reference=None,
                event_code="auth.login.rejected",
                reason_code="invalid_credentials",
                evidence={"attempt": "rejected"},
                run_id=None,
                service_boot_id=None,
                occurred_at=occurred_at,
            )

    await asyncio.gather(
        append(EVENT_ONE, NOW),
        append(EVENT_TWO, NOW + timedelta(microseconds=1)),
    )

    async with uow_factory.begin() as uow:
        events = await uow.observability.list_audit_events()
        report = await uow.observability.verify_audit_chain()

    assert [event.sequence for event in events] == [1, 2]
    assert {event.event_id for event in events} == {EVENT_ONE, EVENT_TWO}
    assert events[0].previous_hash is None
    assert events[1].previous_hash == events[0].content_hash
    assert report.ok is True
    assert report.event_count == 2
    assert report.terminal_hash == events[-1].content_hash


async def test_audit_append_is_exactly_idempotent_but_conflicts_on_changed_evidence(
    uow_factory: UnitOfWork,
) -> None:
    arguments = {
        "event_id": EVENT_ONE,
        "source_role": AuditSourceRole.WEB,
        "actor_reference": pseudonymous_reference("actor", "sole_operator"),
        "session_reference": None,
        "event_code": "auth.login.succeeded",
        "reason_code": "valid_credentials",
        "evidence": {"authentication": "password"},
        "run_id": None,
        "service_boot_id": None,
        "occurred_at": NOW,
    }
    async with uow_factory.begin() as uow:
        first = await uow.observability.append_audit(**arguments)
        repeated = await uow.observability.append_audit(**arguments)
        with pytest.raises(RuntimeError, match="immutable audit identity"):
            await uow.observability.append_audit(
                **{**arguments, "evidence": {"authentication": "changed"}}
            )

    assert first == repeated


async def test_audit_events_cannot_be_updated_or_deleted(
    uow_factory: UnitOfWork,
) -> None:
    async with uow_factory.begin() as uow:
        await uow.observability.append_audit(
            event_id=EVENT_ONE,
            source_role=AuditSourceRole.WEB,
            actor_reference=pseudonymous_reference("actor", "sole_operator"),
            session_reference=None,
            event_code="auth.login.rejected",
            reason_code="invalid_credentials",
            evidence={"attempt": "rejected"},
            run_id=None,
            service_boot_id=None,
            occurred_at=NOW,
        )

    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                update(AuditEventModel)
                .where(AuditEventModel.event_id == EVENT_ONE)
                .values(reason_code="tampered")
            )
    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                delete(AuditEventModel).where(AuditEventModel.event_id == EVENT_ONE)
            )


async def test_health_evaluations_are_hash_verified_exactly_idempotent_and_immutable(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_run(uow_factory, experiment_id=EXPERIMENT_ONE, run_id=RUN_ONE)
    service = _service(
        run_id=RUN_ONE,
        boot_id=OPERATIONS_BOOT,
        role=ServiceRole.OPERATIONS,
        service_id="operations-service",
    )
    async with uow_factory.begin() as uow:
        await uow.platform.register_service_instance(service)

    evaluation = HealthEvaluation.create(
        evaluation_id=EVALUATION_ONE,
        run_id=RUN_ONE,
        service_boot_id=OPERATIONS_BOOT,
        overall_status=HealthStatus.CRITICAL,
        failed_check_names=("worker_lease",),
        severity=HealthSeverity.CRITICAL,
        deduplication_key=health_deduplication_key(RUN_ONE, ("worker_lease",)),
        incident_id=None,
        recovery_of_evaluation_id=None,
        recovered_at=None,
        components={"worker_lease": {"status": "failed", "reason_code": "lease_expired"}},
        checked_at=NOW,
    )
    async with uow_factory.begin() as uow:
        first = await uow.observability.record_health(evaluation)
        repeated = await uow.observability.record_health(evaluation)
        restored = await uow.observability.get_health(EVALUATION_ONE)
        latest = await uow.observability.latest_health(RUN_ONE)

    assert first == repeated == restored == latest == evaluation
    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                update(HealthEvaluationModel)
                .where(HealthEvaluationModel.evaluation_id == EVALUATION_ONE)
                .values(overall_status="healthy")
            )


async def test_health_recovery_snapshot_links_prior_failure_without_mutating_it(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_run(uow_factory, experiment_id=EXPERIMENT_ONE, run_id=RUN_ONE)
    service = _service(
        run_id=RUN_ONE,
        boot_id=OPERATIONS_BOOT,
        role=ServiceRole.OPERATIONS,
        service_id="operations-service",
    )
    async with uow_factory.begin() as uow:
        await uow.platform.register_service_instance(service)

    failure = HealthEvaluation.create(
        evaluation_id=EVALUATION_ONE,
        run_id=RUN_ONE,
        service_boot_id=OPERATIONS_BOOT,
        overall_status=HealthStatus.WARNING,
        failed_check_names=("backup",),
        severity=HealthSeverity.WARNING,
        deduplication_key=health_deduplication_key(RUN_ONE, ("backup",)),
        incident_id=None,
        recovery_of_evaluation_id=None,
        recovered_at=None,
        components={"backup": {"status": "warning", "reason_code": "backup_stale"}},
        checked_at=NOW,
    )
    recovery = HealthEvaluation.create(
        evaluation_id=EVALUATION_TWO,
        run_id=RUN_ONE,
        service_boot_id=OPERATIONS_BOOT,
        overall_status=HealthStatus.HEALTHY,
        failed_check_names=(),
        severity=HealthSeverity.INFO,
        deduplication_key=health_deduplication_key(RUN_ONE, ()),
        incident_id=None,
        recovery_of_evaluation_id=EVALUATION_ONE,
        recovered_at=NOW + timedelta(minutes=1),
        components={"backup": {"status": "ok"}},
        checked_at=NOW + timedelta(minutes=1),
    )
    async with uow_factory.begin() as uow:
        await uow.observability.record_health(failure)
        await uow.observability.record_health(recovery)
        rows = tuple(
            await uow.session.scalars(
                select(HealthEvaluationModel).order_by(HealthEvaluationModel.checked_at)
            )
        )

    assert len(rows) == 2
    assert rows[0].recovery_of_evaluation_id is None
    assert rows[1].recovery_of_evaluation_id == EVALUATION_ONE
