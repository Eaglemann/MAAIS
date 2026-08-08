from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from maais.config.cloud import ServiceRole
from maais.config.constants import ALL_AGENTS
from maais.db.models.ledger import DomainEventModel
from maais.db.models.platform import PlatformCandidateModel, ServiceInstanceModel
from maais.db.repositories.platform import PlatformIdentityConflict, PlatformStateConflict
from maais.db.unit_of_work import UnitOfWork
from maais.operations.operator_commands import CommandType, OperatorCommand
from maais.platform.identity import CandidateDescriptor, RailwayRuntimeIdentity
from maais.platform.registry import (
    CandidateStatus,
    CandidateTransitionError,
    PlatformRun,
    RunPurpose,
    RunStatus,
    RunTransitionError,
    ServiceInstance,
    ServiceInstanceConflict,
)
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
EXPERIMENT_ONE = UUID("11111111-1111-4111-8111-111111111111")
EXPERIMENT_TWO = UUID("22222222-2222-4222-8222-222222222222")
RUN_ONE = UUID("33333333-3333-4333-8333-333333333333")
RUN_TWO = UUID("44444444-4444-4444-8444-444444444444")
COMMAND_ONE = UUID("55555555-5555-4555-8555-555555555555")
COMMAND_TWO = UUID("66666666-6666-4666-8666-666666666666")
WORKER_ONE = UUID("77777777-7777-4777-8777-777777777777")
WORKER_TWO = UUID("88888888-8888-4888-8888-888888888888")


async def test_candidate_registration_is_exactly_idempotent_and_not_event_backed(
    uow_factory: UnitOfWork,
) -> None:
    descriptor = _descriptor()
    async with uow_factory.begin() as uow:
        await uow.experiments.create(_manifest(experiment_id=EXPERIMENT_ONE))
        first = await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
        repeated = await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
        candidate_count = await uow.session.scalar(
            select(func.count()).select_from(PlatformCandidateModel)
        )
        event_count = await uow.session.scalar(select(func.count()).select_from(DomainEventModel))

    assert first == repeated
    assert first.status is CandidateStatus.REGISTERED
    assert candidate_count == 1
    assert event_count == 1  # The experiment event only; registry rows are operational evidence.


async def test_candidate_hash_collision_or_creator_change_is_rejected(
    uow_factory: UnitOfWork,
) -> None:
    descriptor = _descriptor()
    async with uow_factory.begin() as uow:
        await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
    async with uow_factory.begin() as uow:
        with pytest.raises(PlatformIdentityConflict, match="creator"):
            await uow.platform.register_candidate(
                descriptor,
                creator_deployment_id="deployment-2",
                registered_at=NOW,
            )
    async with uow_factory.begin() as uow:
        await uow.session.execute(
            update(PlatformCandidateModel)
            .where(PlatformCandidateModel.descriptor_hash == descriptor.descriptor_hash)
            .values(descriptor_json={"tampered": True})
        )
    async with uow_factory.begin() as uow:
        with pytest.raises(PlatformIdentityConflict, match="descriptor"):
            await uow.platform.register_candidate(
                descriptor,
                creator_deployment_id="deployment-1",
                registered_at=NOW,
            )


async def test_candidate_terminal_evidence_is_frozen(
    uow_factory: UnitOfWork,
) -> None:
    descriptor = _descriptor()
    async with uow_factory.begin() as uow:
        await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
        await uow.platform.begin_candidate_qualification(
            descriptor.descriptor_hash,
            qualifying_at=NOW + timedelta(seconds=1),
        )
        qualified = await uow.platform.qualify_candidate(
            descriptor.descriptor_hash,
            evidence_hash="f" * 64,
            qualified_at=NOW + timedelta(seconds=2),
        )
    async with uow_factory.begin() as uow:
        repeated = await uow.platform.qualify_candidate(
            descriptor.descriptor_hash,
            evidence_hash="f" * 64,
            qualified_at=NOW + timedelta(seconds=2),
        )
        with pytest.raises(CandidateTransitionError, match="terminal"):
            await uow.platform.reject_candidate(
                descriptor.descriptor_hash,
                evidence_hash="e" * 64,
                rejected_at=NOW + timedelta(seconds=3),
            )

    assert qualified == repeated
    assert qualified.status is CandidateStatus.QUALIFIED


async def test_official_run_cannot_be_created_from_an_unqualified_candidate(
    uow_factory: UnitOfWork,
) -> None:
    descriptor = _descriptor()
    manifest = _manifest(experiment_id=EXPERIMENT_ONE, schema_revision="0020")
    run = PlatformRun.create(
        run_id=RUN_ONE,
        experiment_id=EXPERIMENT_ONE,
        candidate_hash=descriptor.descriptor_hash,
        manifest_hash=manifest.manifest_hash,
        database_system_identifier="7669409277984608290",
        railway_environment_id="environment-1",
        purpose=RunPurpose.SOAK,
        created_at=NOW,
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
        with pytest.raises(PlatformStateConflict, match="qualified candidate"):
            await uow.platform.create_run(run)


async def test_run_activation_requires_command_and_registered_worker_boot(
    uow_factory: UnitOfWork,
) -> None:
    run = await _prepare_run(uow_factory, experiment_id=EXPERIMENT_ONE, run_id=RUN_ONE)
    async with uow_factory.begin() as uow:
        with pytest.raises(RunTransitionError, match="operator command"):
            await uow.platform.activate_run(
                RUN_ONE,
                command_id=None,
                worker_boot_id=WORKER_ONE,
                started_at=NOW + timedelta(seconds=3),
            )
        with pytest.raises(RunTransitionError, match="worker boot"):
            await uow.platform.activate_run(
                RUN_ONE,
                command_id=COMMAND_ONE,
                worker_boot_id=None,
                started_at=NOW + timedelta(seconds=3),
            )
        with pytest.raises(PlatformStateConflict, match="operator command"):
            await uow.platform.activate_run(
                RUN_ONE,
                command_id=COMMAND_ONE,
                worker_boot_id=WORKER_ONE,
                started_at=NOW + timedelta(seconds=3),
            )
        await uow.commands.enqueue(_start_command(EXPERIMENT_ONE, RUN_ONE, COMMAND_ONE))
        await uow.commands.claim_next(
            EXPERIMENT_ONE,
            worker_id=f"paper_worker:{WORKER_ONE}",
            accepted_at=NOW + timedelta(seconds=2),
        )
        with pytest.raises(PlatformStateConflict, match="worker boot"):
            await uow.platform.activate_run(
                RUN_ONE,
                command_id=COMMAND_ONE,
                worker_boot_id=WORKER_ONE,
                started_at=NOW + timedelta(seconds=3),
            )
        await uow.platform.register_service_instance(_service(run_id=RUN_ONE, boot_id=WORKER_TWO))
        with pytest.raises(PlatformStateConflict, match="accepted worker"):
            await uow.platform.activate_run(
                RUN_ONE,
                command_id=COMMAND_ONE,
                worker_boot_id=WORKER_TWO,
                started_at=NOW + timedelta(seconds=3),
            )

    assert run.status is RunStatus.STANDBY


async def test_database_rejects_a_second_active_run_in_one_environment(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_TWO,
        run_id=RUN_TWO,
        command_id=COMMAND_TWO,
        worker_boot_id=WORKER_TWO,
    )
    async with uow_factory.begin() as uow:
        active = await uow.platform.activate_run(
            RUN_ONE,
            command_id=COMMAND_ONE,
            worker_boot_id=WORKER_ONE,
            started_at=NOW + timedelta(seconds=3),
        )
    async with uow_factory.begin() as uow:
        with pytest.raises(PlatformStateConflict, match="active run"):
            await uow.platform.activate_run(
                RUN_TWO,
                command_id=COMMAND_TWO,
                worker_boot_id=WORKER_TWO,
                started_at=NOW + timedelta(seconds=3),
            )

    assert active.status is RunStatus.ACTIVE


async def test_service_registration_is_idempotent_but_boot_identity_is_immutable(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_run(uow_factory, experiment_id=EXPERIMENT_ONE, run_id=RUN_ONE)
    instance = _service(run_id=RUN_ONE, boot_id=WORKER_ONE)
    async with uow_factory.begin() as uow:
        first = await uow.platform.register_service_instance(instance)
        repeated = await uow.platform.register_service_instance(instance)
        count = await uow.session.scalar(select(func.count()).select_from(ServiceInstanceModel))
    changed = replace(
        instance,
        identity=replace(instance.identity, deployment_id="deployment-changed"),
    )
    async with uow_factory.begin() as uow:
        with pytest.raises(PlatformIdentityConflict, match="boot identity"):
            await uow.platform.register_service_instance(changed)

    assert first == repeated
    assert count == 1


async def test_heartbeat_is_monotonic_and_terminal_stop_is_durable(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_run(uow_factory, experiment_id=EXPERIMENT_ONE, run_id=RUN_ONE)
    instance = _service(run_id=RUN_ONE, boot_id=WORKER_ONE)
    async with uow_factory.begin() as uow:
        await uow.platform.register_service_instance(instance)
        heartbeat = await uow.platform.heartbeat_service_instance(
            boot_id=WORKER_ONE,
            sequence=1,
            heartbeat_at=NOW + timedelta(seconds=2),
        )
    async with uow_factory.begin() as uow:
        with pytest.raises(ServiceInstanceConflict, match="sequence"):
            await uow.platform.heartbeat_service_instance(
                boot_id=WORKER_ONE,
                sequence=1,
                heartbeat_at=NOW + timedelta(seconds=3),
            )
    async with uow_factory.begin() as uow:
        stopped = await uow.platform.stop_service_instance(
            boot_id=WORKER_ONE,
            reason="clean_shutdown",
            stopped_at=NOW + timedelta(seconds=3),
        )
    async with uow_factory.begin() as uow:
        with pytest.raises(ServiceInstanceConflict, match="stopped"):
            await uow.platform.heartbeat_service_instance(
                boot_id=WORKER_ONE,
                sequence=2,
                heartbeat_at=NOW + timedelta(seconds=4),
            )

    assert heartbeat.heartbeat_sequence == 1
    assert stopped.terminal_reason == "clean_shutdown"


async def test_run_invalidation_and_service_continuity_query_are_durable(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )
    web = _service(
        run_id=RUN_ONE,
        boot_id=UUID("99999999-9999-4999-8999-999999999999"),
        role=ServiceRole.WEB,
        service_id="web-service",
    )
    async with uow_factory.begin() as uow:
        await uow.platform.register_service_instance(web)
        await uow.platform.activate_run(
            RUN_ONE,
            command_id=COMMAND_ONE,
            worker_boot_id=WORKER_ONE,
            started_at=NOW + timedelta(seconds=3),
        )
        invalidated = await uow.platform.invalidate_run(
            RUN_ONE,
            reason="worker_restarted",
            invalidated_at=NOW + timedelta(seconds=4),
        )
    async with uow_factory.begin() as uow:
        restored = await uow.platform.get_run(RUN_ONE)
        services = await uow.platform.list_run_services(RUN_ONE)
        with pytest.raises(RunTransitionError, match="invalidated"):
            await uow.platform.complete_run(RUN_ONE)

    assert invalidated == restored
    assert restored.continuity_invalidated is True
    assert [service.identity.service_role for service in services] == [
        ServiceRole.WEB,
        ServiceRole.WORKER,
    ]


async def _prepare_run(
    uow_factory: UnitOfWork,
    *,
    experiment_id: UUID,
    run_id: UUID,
) -> PlatformRun:
    descriptor = _descriptor()
    manifest = _manifest(experiment_id=experiment_id, schema_revision="0020")
    run = PlatformRun.create(
        run_id=run_id,
        experiment_id=experiment_id,
        candidate_hash=descriptor.descriptor_hash,
        manifest_hash=manifest.manifest_hash,
        database_system_identifier="7669409277984608290",
        railway_environment_id="environment-1",
        purpose=RunPurpose.SOAK,
        created_at=NOW,
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        candidate = await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
        if candidate.status is CandidateStatus.REGISTERED:
            await uow.platform.begin_candidate_qualification(
                descriptor.descriptor_hash,
                qualifying_at=NOW + timedelta(microseconds=1),
            )
            await uow.platform.qualify_candidate(
                descriptor.descriptor_hash,
                evidence_hash="f" * 64,
                qualified_at=NOW + timedelta(microseconds=2),
            )
        return await uow.platform.create_run(run)


async def _prepare_activatable_run(
    uow_factory: UnitOfWork,
    *,
    experiment_id: UUID,
    run_id: UUID,
    command_id: UUID,
    worker_boot_id: UUID,
) -> None:
    await _prepare_run(uow_factory, experiment_id=experiment_id, run_id=run_id)
    command = _start_command(experiment_id, run_id, command_id)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(command)
        await uow.commands.claim_next(
            experiment_id,
            worker_id=f"paper_worker:{worker_boot_id}",
            accepted_at=NOW + timedelta(seconds=2),
        )
        await uow.platform.register_service_instance(
            _service(run_id=run_id, boot_id=worker_boot_id)
        )


def _start_command(
    experiment_id: UUID,
    run_id: UUID,
    command_id: UUID,
) -> OperatorCommand:
    return OperatorCommand.request(
        command_id=command_id,
        experiment_id=experiment_id,
        command_type=CommandType.START,
        idempotency_key=str(command_id),
        actor="sole_operator",
        reason="start approved paper run",
        payload={"run_id": str(run_id)},
        confirmation="CONFIRM START",
        requested_at=NOW + timedelta(seconds=1),
    )


def _descriptor() -> CandidateDescriptor:
    return CandidateDescriptor.build(
        git_sha="a" * 40,
        source_clean=True,
        uv_lock_sha256="b" * 64,
        dashboard_lock_sha256="c" * 64,
        schema_revision="0020",
        agent_implementation_hashes={
            name: f"{index + 1:064x}" for index, name in enumerate(ALL_AGENTS)
        },
        dashboard_asset_manifest_sha256="d" * 64,
        build_definition_sha256="e" * 64,
    )


def _service(
    *,
    run_id: UUID,
    boot_id: UUID,
    role: ServiceRole = ServiceRole.WORKER,
    service_id: str = "worker-service",
) -> ServiceInstance:
    identity = RailwayRuntimeIdentity(
        project_id="project-1",
        environment_id="environment-1",
        service_id=service_id,
        deployment_id="deployment-1",
        snapshot_id=None,
        replica_id=f"replica-{boot_id}",
        region="europe-west4",
        service_role=role,
        boot_id=boot_id,
        candidate_hash=_descriptor().descriptor_hash,
        started_at=NOW,
    )
    return ServiceInstance.register(
        identity,
        run_id=run_id,
        first_seen_at=NOW + timedelta(seconds=1),
    )
