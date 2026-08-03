from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.models.operations import OperatorCommandModel
from maais.db.replay import verify_ledger_consistency
from maais.db.unit_of_work import UnitOfWork
from maais.operations.operator_commands import CommandType, OperatorCommand
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


def _command(
    command_id: UUID,
    *,
    idempotency_key: str = "33333333-3333-4333-8333-333333333333",
    requested_at: datetime = NOW,
) -> OperatorCommand:
    return OperatorCommand.request(
        command_id=command_id,
        experiment_id=EXPERIMENT_ID,
        command_type=CommandType.EMERGENCY_HALT,
        idempotency_key=idempotency_key,
        actor="local_operator",
        reason="operator observed abnormal behavior",
        payload={"source": "mission_control"},
        confirmation="CONFIRM EMERGENCY_HALT",
        requested_at=requested_at,
    )


async def test_command_request_is_event_backed_and_idempotent_by_request_identity(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    first = _command(UUID("11111111-1111-4111-8111-111111111111"))
    retry = _command(UUID("44444444-4444-4444-8444-444444444444"))
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        created = await uow.commands.enqueue(first)
    async with uow_factory.begin() as uow:
        repeated = await uow.commands.enqueue(retry)
        restored = await uow.commands.get(first.command_id)
        consistency = await verify_ledger_consistency(uow.session)
        command_count = await uow.session.scalar(
            select(func.count()).select_from(OperatorCommandModel)
        )
        event_types = tuple(
            await uow.session.scalars(
                select(DomainEventModel.event_type)
                .where(
                    DomainEventModel.aggregate_type == "operator_command",
                    DomainEventModel.aggregate_id == first.command_id,
                )
                .order_by(DomainEventModel.stream_version)
            )
        )
        outbox_count = await uow.session.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .join(DomainEventModel, DomainEventModel.id == OutboxEventModel.domain_event_id)
            .where(DomainEventModel.aggregate_type == "operator_command")
        )

    assert created.created is True
    assert created.command == first
    assert repeated.created is False
    assert repeated.command == first
    assert restored == first
    assert command_count == 1
    assert event_types == ("operator_command.requested",)
    assert outbox_count == 1
    assert consistency.ok


async def test_worker_claims_the_oldest_request_and_records_its_identity(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    oldest = _command(UUID("11111111-1111-4111-8111-111111111111"))
    newer = _command(
        UUID("44444444-4444-4444-8444-444444444444"),
        idempotency_key="55555555-5555-4555-8555-555555555555",
        requested_at=NOW + timedelta(seconds=1),
    )
    worker_id = "paper_worker:66666666-6666-4666-8666-666666666666"
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.commands.enqueue(newer)
        await uow.commands.enqueue(oldest)
    async with uow_factory.begin() as uow:
        claimed = await uow.commands.claim_next(
            EXPERIMENT_ID,
            worker_id=worker_id,
            accepted_at=NOW + timedelta(seconds=2),
        )
        restored = await uow.commands.get(oldest.command_id)
        event_types = tuple(
            await uow.session.scalars(
                select(DomainEventModel.event_type)
                .where(
                    DomainEventModel.aggregate_type == "operator_command",
                    DomainEventModel.aggregate_id == oldest.command_id,
                )
                .order_by(DomainEventModel.stream_version)
            )
        )

    assert claimed == restored
    assert claimed is not None
    assert claimed.command_id == oldest.command_id
    assert claimed.accepted_by == worker_id
    assert event_types == (
        "operator_command.requested",
        "operator_command.accepted",
    )


async def test_replacement_worker_recovers_accepted_command_before_new_requests(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    accepted_request = _command(UUID("11111111-1111-4111-8111-111111111111"))
    newer = _command(
        UUID("44444444-4444-4444-8444-444444444444"),
        idempotency_key="55555555-5555-4555-8555-555555555555",
        requested_at=NOW + timedelta(seconds=1),
    )
    original_worker = "paper_worker:66666666-6666-4666-8666-666666666666"
    replacement_worker = "paper_worker:77777777-7777-4777-8777-777777777777"
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.commands.enqueue(accepted_request)
        await uow.commands.enqueue(newer)
        await uow.commands.claim_next(
            EXPERIMENT_ID,
            worker_id=original_worker,
            accepted_at=NOW + timedelta(seconds=2),
        )

    async with uow_factory.begin() as uow:
        recovered = await uow.commands.claim_next(
            EXPERIMENT_ID,
            worker_id=replacement_worker,
            accepted_at=NOW + timedelta(seconds=3),
        )
        event_types = tuple(
            await uow.session.scalars(
                select(DomainEventModel.event_type)
                .where(
                    DomainEventModel.aggregate_type == "operator_command",
                    DomainEventModel.aggregate_id == accepted_request.command_id,
                )
                .order_by(DomainEventModel.stream_version)
            )
        )

    assert recovered is not None
    assert recovered.command_id == accepted_request.command_id
    assert recovered.accepted_by == replacement_worker
    assert recovered.accepted_at == NOW + timedelta(seconds=2)
    assert recovered.version == 3
    assert event_types == (
        "operator_command.requested",
        "operator_command.accepted",
        "operator_command.recovered",
    )


async def test_worker_completion_is_persistent_event_backed_and_idempotent(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    requested = _command(UUID("11111111-1111-4111-8111-111111111111"))
    worker_id = "paper_worker:66666666-6666-4666-8666-666666666666"
    result = {"kill_switch_active": True, "experiment_status": "paused"}
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.commands.enqueue(requested)
        await uow.commands.claim_next(
            EXPERIMENT_ID,
            worker_id=worker_id,
            accepted_at=NOW + timedelta(seconds=1),
        )
    async with uow_factory.begin() as uow:
        completed = await uow.commands.complete(
            requested.command_id,
            worker_id=worker_id,
            completed_at=NOW + timedelta(seconds=2),
            result=result,
        )
    async with uow_factory.begin() as uow:
        repeated = await uow.commands.complete(
            requested.command_id,
            worker_id=worker_id,
            completed_at=NOW + timedelta(seconds=3),
            result=result,
        )
        restored = await uow.commands.get(requested.command_id)
        event_types = tuple(
            await uow.session.scalars(
                select(DomainEventModel.event_type)
                .where(
                    DomainEventModel.aggregate_type == "operator_command",
                    DomainEventModel.aggregate_id == requested.command_id,
                )
                .order_by(DomainEventModel.stream_version)
            )
        )
        consistency = await verify_ledger_consistency(uow.session)

    assert completed == restored
    assert repeated == completed
    assert restored.result == result
    assert event_types == (
        "operator_command.requested",
        "operator_command.accepted",
        "operator_command.completed",
    )
    assert consistency.ok


async def test_worker_rejection_preserves_structured_reason_and_audit_event(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    requested = _command(UUID("11111111-1111-4111-8111-111111111111"))
    worker_id = "paper_worker:66666666-6666-4666-8666-666666666666"
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.commands.enqueue(requested)
        await uow.commands.claim_next(
            EXPERIMENT_ID,
            worker_id=worker_id,
            accepted_at=NOW + timedelta(seconds=1),
        )
    async with uow_factory.begin() as uow:
        rejected = await uow.commands.reject(
            requested.command_id,
            worker_id=worker_id,
            completed_at=NOW + timedelta(seconds=2),
            reason_code="unsafe_reset",
            detail="an open simulated position prevents kill-switch reset",
        )
        event_types = tuple(
            await uow.session.scalars(
                select(DomainEventModel.event_type)
                .where(
                    DomainEventModel.aggregate_type == "operator_command",
                    DomainEventModel.aggregate_id == requested.command_id,
                )
                .order_by(DomainEventModel.stream_version)
            )
        )

    assert rejected.result == {
        "reason_code": "unsafe_reset",
        "detail": "an open simulated position prevents kill-switch reset",
    }
    assert event_types[-1] == "operator_command.rejected"


async def test_ledger_verifier_detects_operator_command_projection_tampering(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    requested = _command(UUID("11111111-1111-4111-8111-111111111111"))
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.commands.enqueue(requested)
    async with uow_factory.begin() as uow:
        await uow.session.execute(
            update(OperatorCommandModel)
            .where(OperatorCommandModel.id == requested.command_id)
            .values(content_hash="0" * 64)
        )
        report = await verify_ledger_consistency(uow.session)

    assert [error.code for error in report.errors] == ["operator_command_projection_mismatch"]
