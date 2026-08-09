"""Transactional authority for cloud candidates, official runs, and service boots."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from maais.config.cloud import ServiceRole
from maais.db.models.experiments import ExperimentModel
from maais.db.models.operations import OperatorCommandModel
from maais.db.models.platform import (
    PlatformCandidateModel,
    RunInstanceModel,
    ServiceInstanceModel,
)
from maais.operations.operator_commands import CommandStatus, CommandType
from maais.platform.identity import CandidateDescriptor, RailwayRuntimeIdentity
from maais.platform.registry import (
    CandidateStatus,
    CandidateTransitionError,
    PlatformCandidate,
    PlatformRun,
    RunPurpose,
    RunStatus,
    ServiceInstance,
)


class PlatformIdentityConflict(RuntimeError):
    """A durable identifier was reused with different immutable evidence."""


class PlatformStateConflict(RuntimeError):
    """A requested registry transition conflicts with authoritative state."""


class PlatformRepository:
    """Persist platform evidence without authority to mutate the trading event ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register_candidate(
        self,
        descriptor: CandidateDescriptor,
        *,
        creator_deployment_id: str,
        registered_at: datetime,
    ) -> PlatformCandidate:
        candidate = PlatformCandidate.register(
            descriptor,
            creator_deployment_id=creator_deployment_id,
            registered_at=registered_at,
        )
        created_hash = await self._session.scalar(
            insert(PlatformCandidateModel)
            .values(**_candidate_values(candidate))
            .on_conflict_do_nothing()
            .returning(PlatformCandidateModel.descriptor_hash)
        )
        if created_hash is not None:
            return candidate

        row = await self._locked_candidate(descriptor.descriptor_hash)
        if row.creator_deployment_id != creator_deployment_id:
            raise PlatformIdentityConflict("candidate creator deployment identity has changed")
        if row.descriptor_json != descriptor.to_json_data():
            raise PlatformIdentityConflict("candidate descriptor hash collision detected")
        try:
            existing = _candidate_from_row(row)
        except ValueError as exc:
            raise PlatformIdentityConflict("candidate descriptor evidence is invalid") from exc
        if existing.registered_at != registered_at:
            raise PlatformIdentityConflict("candidate registration evidence has changed")
        return existing

    async def begin_candidate_qualification(
        self,
        candidate_hash: str,
        *,
        qualifying_at: datetime,
    ) -> PlatformCandidate:
        row = await self._locked_candidate(candidate_hash)
        current = _candidate_from_row(row)
        if current.status is CandidateStatus.QUALIFYING and current.qualifying_at == qualifying_at:
            return current
        updated = current.begin_qualification(qualifying_at)
        _write_candidate_lifecycle(row, updated)
        return updated

    async def qualify_candidate(
        self,
        candidate_hash: str,
        *,
        evidence_hash: str,
        qualified_at: datetime,
    ) -> PlatformCandidate:
        return await self._finish_candidate(
            candidate_hash,
            status=CandidateStatus.QUALIFIED,
            evidence_hash=evidence_hash,
            terminal_at=qualified_at,
        )

    async def reject_candidate(
        self,
        candidate_hash: str,
        *,
        evidence_hash: str,
        rejected_at: datetime,
    ) -> PlatformCandidate:
        return await self._finish_candidate(
            candidate_hash,
            status=CandidateStatus.REJECTED,
            evidence_hash=evidence_hash,
            terminal_at=rejected_at,
        )

    async def create_run(self, run: PlatformRun) -> PlatformRun:
        if run.status is not RunStatus.STANDBY:
            raise ValueError("new platform run must be in standby")
        experiment = await self._session.get(ExperimentModel, run.experiment_id)
        if experiment is None:
            raise PlatformStateConflict("run experiment does not exist")
        if experiment.manifest_hash != run.manifest_hash:
            raise PlatformIdentityConflict("run manifest does not match its experiment")
        candidate = await self._session.get(PlatformCandidateModel, run.candidate_hash)
        if candidate is None:
            raise PlatformStateConflict("run candidate does not exist")
        if candidate.status == CandidateStatus.REJECTED.value:
            raise PlatformStateConflict("rejected candidate cannot create a run")
        if (
            run.purpose in (RunPurpose.SOAK, RunPurpose.SEVEN_DAY)
            and candidate.status != CandidateStatus.QUALIFIED.value
        ):
            raise PlatformStateConflict("official run requires a qualified candidate")

        created_id = await self._session.scalar(
            insert(RunInstanceModel)
            .values(**_run_values(run))
            .on_conflict_do_nothing()
            .returning(RunInstanceModel.id)
        )
        if created_id is not None:
            return run
        row = await self._session.scalar(
            select(RunInstanceModel).where(RunInstanceModel.id == run.id).with_for_update()
        )
        if row is None:
            raise PlatformIdentityConflict("run experiment already belongs to another run")
        existing = _run_from_row(row)
        if existing != run:
            raise PlatformIdentityConflict("run immutable registration evidence has changed")
        return existing

    async def activate_run(
        self,
        run_id: UUID,
        *,
        command_id: UUID | None,
        worker_boot_id: UUID | None,
        started_at: datetime,
    ) -> PlatformRun:
        row = await self._locked_run(run_id)
        current = _run_from_row(row)
        if (
            current.status is RunStatus.ACTIVE
            and current.requested_operator_command_id == command_id
            and current.activating_worker_boot_id == worker_boot_id
            and current.started_at == started_at
        ):
            return current
        updated = current.activate(
            command_id=command_id,
            worker_boot_id=worker_boot_id,
            started_at=started_at,
        )
        assert command_id is not None
        assert worker_boot_id is not None
        command = await self._session.scalar(
            select(OperatorCommandModel)
            .where(OperatorCommandModel.id == command_id)
            .with_for_update()
        )
        if command is None:
            raise PlatformStateConflict("activation operator command does not exist")
        if (
            command.experiment_id != current.experiment_id
            or command.command_type != CommandType.START.value
            or command.status != CommandStatus.ACCEPTED.value
            or not command.operator_confirmed
        ):
            raise PlatformStateConflict(
                "activation requires the accepted, confirmed start operator command"
            )
        if command.accepted_by != f"paper_worker:{worker_boot_id}":
            raise PlatformStateConflict("activation command belongs to another accepted worker")

        service_row = await self._session.scalar(
            select(ServiceInstanceModel)
            .where(ServiceInstanceModel.boot_id == worker_boot_id)
            .with_for_update()
        )
        if service_row is None:
            raise PlatformStateConflict("activation worker boot is not registered")
        service = _service_from_row(service_row)
        if (
            service.run_id != run_id
            or service.identity.service_role is not ServiceRole.WORKER
            or service.identity.candidate_hash != current.candidate_hash
            or service.identity.environment_id != current.railway_environment_id
            or service.stopped_at is not None
        ):
            raise PlatformStateConflict("activation worker boot identity does not match the run")

        other_active = await self._session.scalar(
            select(RunInstanceModel.id)
            .where(
                RunInstanceModel.railway_environment_id == current.railway_environment_id,
                RunInstanceModel.status == RunStatus.ACTIVE.value,
                RunInstanceModel.id != current.id,
            )
            .with_for_update()
        )
        if other_active is not None:
            raise PlatformStateConflict("Railway environment already has an active run")
        _write_run(row, updated)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise PlatformStateConflict(
                "run activation violates an authoritative identity"
            ) from exc
        return updated

    async def invalidate_run(
        self,
        run_id: UUID,
        *,
        reason: str,
        invalidated_at: datetime,
    ) -> PlatformRun:
        row = await self._locked_run(run_id)
        updated = _run_from_row(row).invalidate(reason, invalidated_at)
        _write_run(row, updated)
        return updated

    async def complete_run(self, run_id: UUID) -> PlatformRun:
        row = await self._locked_run(run_id)
        current = _run_from_row(row)
        if current.status is RunStatus.COMPLETED:
            return current
        updated = current.complete()
        _write_run(row, updated)
        return updated

    async def register_service_instance(self, instance: ServiceInstance) -> ServiceInstance:
        if instance.run_id is not None:
            run = await self.get_run(instance.run_id)
            if (
                instance.identity.candidate_hash != run.candidate_hash
                or instance.identity.environment_id != run.railway_environment_id
            ):
                raise PlatformIdentityConflict("service identity does not match its run")
        if (
            await self._session.get(
                PlatformCandidateModel,
                instance.identity.candidate_hash,
            )
            is None
        ):
            raise PlatformStateConflict("service candidate does not exist")

        created_id = await self._session.scalar(
            insert(ServiceInstanceModel)
            .values(**_service_values(instance))
            .on_conflict_do_nothing()
            .returning(ServiceInstanceModel.boot_id)
        )
        if created_id is not None:
            return instance
        row = await self._locked_service(instance.boot_id)
        try:
            existing = _service_from_row(row)
        except (ValueError, PlatformIdentityConflict) as exc:
            raise PlatformIdentityConflict("stored service boot identity is invalid") from exc
        if existing != instance:
            raise PlatformIdentityConflict("service boot identity has changed")
        return existing

    async def heartbeat_service_instance(
        self,
        *,
        boot_id: UUID,
        sequence: int,
        heartbeat_at: datetime,
    ) -> ServiceInstance:
        row = await self._locked_service(boot_id)
        updated = _service_from_row(row).heartbeat(
            sequence=sequence,
            heartbeat_at=heartbeat_at,
        )
        row.heartbeat_sequence = updated.heartbeat_sequence
        row.last_heartbeat_at = updated.last_heartbeat_at
        return updated

    async def stop_service_instance(
        self,
        *,
        boot_id: UUID,
        reason: str,
        stopped_at: datetime,
    ) -> ServiceInstance:
        row = await self._locked_service(boot_id)
        updated = _service_from_row(row).stop(reason, stopped_at)
        row.stopped_at = updated.stopped_at
        row.terminal_reason = updated.terminal_reason
        return updated

    async def get_run(self, run_id: UUID) -> PlatformRun:
        row = await self._session.get(RunInstanceModel, run_id)
        if row is None:
            raise LookupError("platform run does not exist")
        return _run_from_row(row)

    async def get_active_run(self, railway_environment_id: str) -> PlatformRun | None:
        if not railway_environment_id or railway_environment_id != railway_environment_id.strip():
            raise ValueError("Railway environment ID must be nonempty and trimmed")
        row = await self._session.scalar(
            select(RunInstanceModel).where(
                RunInstanceModel.railway_environment_id == railway_environment_id,
                RunInstanceModel.status == RunStatus.ACTIVE.value,
            )
        )
        return _run_from_row(row) if row is not None else None

    async def list_run_services(self, run_id: UUID) -> tuple[ServiceInstance, ...]:
        rows = (
            await self._session.scalars(
                select(ServiceInstanceModel)
                .where(ServiceInstanceModel.run_id == run_id)
                .order_by(
                    ServiceInstanceModel.service_role,
                    ServiceInstanceModel.first_seen_at,
                    ServiceInstanceModel.boot_id,
                )
            )
        ).all()
        return tuple(_service_from_row(row) for row in rows)

    async def _finish_candidate(
        self,
        candidate_hash: str,
        *,
        status: CandidateStatus,
        evidence_hash: str,
        terminal_at: datetime,
    ) -> PlatformCandidate:
        row = await self._locked_candidate(candidate_hash)
        current = _candidate_from_row(row)
        if (
            current.status is status
            and current.qualification_evidence_hash == evidence_hash
            and current.qualified_at == terminal_at
        ):
            return current
        if status is CandidateStatus.QUALIFIED:
            updated = current.qualify(evidence_hash, terminal_at)
        elif status is CandidateStatus.REJECTED:
            updated = current.reject(evidence_hash, terminal_at)
        else:  # pragma: no cover - private call contract
            raise CandidateTransitionError("candidate terminal status is invalid")
        _write_candidate_lifecycle(row, updated)
        return updated

    async def _locked_candidate(self, candidate_hash: str) -> PlatformCandidateModel:
        row = await self._session.scalar(
            select(PlatformCandidateModel)
            .where(PlatformCandidateModel.descriptor_hash == candidate_hash)
            .with_for_update()
        )
        if row is None:
            raise LookupError("platform candidate does not exist")
        return row

    async def _locked_run(self, run_id: UUID) -> RunInstanceModel:
        row = await self._session.scalar(
            select(RunInstanceModel).where(RunInstanceModel.id == run_id).with_for_update()
        )
        if row is None:
            raise LookupError("platform run does not exist")
        return row

    async def _locked_service(self, boot_id: UUID) -> ServiceInstanceModel:
        row = await self._session.scalar(
            select(ServiceInstanceModel)
            .where(ServiceInstanceModel.boot_id == boot_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("service instance does not exist")
        return row


def _candidate_values(candidate: PlatformCandidate) -> dict[str, object]:
    return {
        "descriptor_hash": candidate.descriptor.descriptor_hash,
        "git_sha": candidate.descriptor.git_sha,
        "schema_revision": candidate.descriptor.schema_revision,
        "descriptor_json": candidate.descriptor.to_json_data(),
        "status": candidate.status.value,
        "creator_deployment_id": candidate.creator_deployment_id,
        "registered_at": candidate.registered_at,
        "qualifying_at": candidate.qualifying_at,
        "qualified_at": candidate.qualified_at,
        "qualification_evidence_hash": candidate.qualification_evidence_hash,
    }


def _candidate_from_row(row: PlatformCandidateModel) -> PlatformCandidate:
    descriptor = CandidateDescriptor.from_json_data(row.descriptor_json)
    if (
        descriptor.descriptor_hash != row.descriptor_hash
        or descriptor.git_sha != row.git_sha
        or descriptor.schema_revision != row.schema_revision
    ):
        raise PlatformIdentityConflict("candidate descriptor columns do not match JSON evidence")
    return PlatformCandidate(
        descriptor=descriptor,
        status=CandidateStatus(row.status),
        creator_deployment_id=row.creator_deployment_id,
        registered_at=row.registered_at,
        qualifying_at=row.qualifying_at,
        qualified_at=row.qualified_at,
        qualification_evidence_hash=row.qualification_evidence_hash,
    )


def _write_candidate_lifecycle(
    row: PlatformCandidateModel,
    candidate: PlatformCandidate,
) -> None:
    row.status = candidate.status.value
    row.qualifying_at = candidate.qualifying_at
    row.qualified_at = candidate.qualified_at
    row.qualification_evidence_hash = candidate.qualification_evidence_hash


def _run_values(run: PlatformRun) -> dict[str, object]:
    return {
        "id": run.id,
        "experiment_id": run.experiment_id,
        "candidate_hash": run.candidate_hash,
        "manifest_hash": run.manifest_hash,
        "database_system_identifier": run.database_system_identifier,
        "railway_environment_id": run.railway_environment_id,
        "purpose": run.purpose.value,
        "status": run.status.value,
        "requested_operator_command_id": run.requested_operator_command_id,
        "activating_worker_boot_id": run.activating_worker_boot_id,
        "continuity_invalidated": run.continuity_invalidated,
        "started_at": run.started_at,
        "invalidated_at": run.invalidated_at,
        "invalidation_reason": run.invalidation_reason,
        "created_at": run.created_at,
    }


def _run_from_row(row: RunInstanceModel) -> PlatformRun:
    return PlatformRun(
        id=row.id,
        experiment_id=row.experiment_id,
        candidate_hash=row.candidate_hash,
        manifest_hash=row.manifest_hash,
        database_system_identifier=row.database_system_identifier,
        railway_environment_id=row.railway_environment_id,
        purpose=RunPurpose(row.purpose),
        status=RunStatus(row.status),
        requested_operator_command_id=row.requested_operator_command_id,
        activating_worker_boot_id=row.activating_worker_boot_id,
        continuity_invalidated=row.continuity_invalidated,
        started_at=row.started_at,
        invalidated_at=row.invalidated_at,
        invalidation_reason=row.invalidation_reason,
        created_at=row.created_at,
    )


def _write_run(row: RunInstanceModel, run: PlatformRun) -> None:
    row.status = run.status.value
    row.requested_operator_command_id = run.requested_operator_command_id
    row.activating_worker_boot_id = run.activating_worker_boot_id
    row.continuity_invalidated = run.continuity_invalidated
    row.started_at = run.started_at
    row.invalidated_at = run.invalidated_at
    row.invalidation_reason = run.invalidation_reason


def _service_values(instance: ServiceInstance) -> dict[str, object]:
    identity = instance.identity
    return {
        "boot_id": instance.boot_id,
        "run_id": instance.run_id,
        "project_id": identity.project_id,
        "environment_id": identity.environment_id,
        "service_id": identity.service_id,
        "deployment_id": identity.deployment_id,
        "snapshot_id": identity.snapshot_id,
        "replica_id": identity.replica_id,
        "region": identity.region,
        "service_role": identity.service_role.value,
        "candidate_hash": identity.candidate_hash,
        "runtime_identity_json": identity.to_json_data(),
        "started_at": identity.started_at,
        "first_seen_at": instance.first_seen_at,
        "last_heartbeat_at": instance.last_heartbeat_at,
        "heartbeat_sequence": instance.heartbeat_sequence,
        "stopped_at": instance.stopped_at,
        "terminal_reason": instance.terminal_reason,
    }


def _service_from_row(row: ServiceInstanceModel) -> ServiceInstance:
    identity = RailwayRuntimeIdentity(
        project_id=row.project_id,
        environment_id=row.environment_id,
        service_id=row.service_id,
        deployment_id=row.deployment_id,
        snapshot_id=row.snapshot_id,
        replica_id=row.replica_id,
        region=row.region,
        service_role=ServiceRole(row.service_role),
        boot_id=row.boot_id,
        candidate_hash=row.candidate_hash,
        started_at=row.started_at,
    )
    if row.runtime_identity_json != identity.to_json_data():
        raise PlatformIdentityConflict("service runtime columns do not match JSON evidence")
    return ServiceInstance(
        boot_id=row.boot_id,
        run_id=row.run_id,
        identity=identity,
        first_seen_at=row.first_seen_at,
        last_heartbeat_at=row.last_heartbeat_at,
        heartbeat_sequence=row.heartbeat_sequence,
        stopped_at=row.stopped_at,
        terminal_reason=row.terminal_reason,
    )
