"""Pure, monotonic lifecycle models for cloud candidates, runs, and service boots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from maais.platform.identity import CandidateDescriptor, RailwayRuntimeIdentity


class CandidateStatus(StrEnum):
    REGISTERED = "registered"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    REJECTED = "rejected"


class RunPurpose(StrEnum):
    PROCESS_DRILL = "process_drill"
    SOAK = "soak"
    SEVEN_DAY = "seven_day"


class RunStatus(StrEnum):
    STANDBY = "standby"
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    COMPLETED = "completed"


class CandidateTransitionError(RuntimeError):
    pass


class RunTransitionError(RuntimeError):
    pass


class ServiceInstanceConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlatformCandidate:
    descriptor: CandidateDescriptor
    status: CandidateStatus
    creator_deployment_id: str
    registered_at: datetime
    qualifying_at: datetime | None
    qualified_at: datetime | None
    qualification_evidence_hash: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, CandidateDescriptor):
            raise ValueError("descriptor must be a CandidateDescriptor")
        if not isinstance(self.status, CandidateStatus):
            raise ValueError("status must be a CandidateStatus")
        _trimmed("creator_deployment_id", self.creator_deployment_id)
        _utc(self.registered_at, "registered_at")
        if self.qualifying_at is not None:
            _utc(self.qualifying_at, "qualifying_at")
            if self.qualifying_at < self.registered_at:
                raise ValueError("qualifying_at cannot precede registered_at")
        if self.qualified_at is not None:
            _utc(self.qualified_at, "qualified_at")
            if self.qualifying_at is None or self.qualified_at < self.qualifying_at:
                raise ValueError("qualified_at cannot precede qualifying_at")
        if self.qualification_evidence_hash is not None:
            _hash("qualification_evidence_hash", self.qualification_evidence_hash)
        if self.status is CandidateStatus.REGISTERED and any(
            value is not None
            for value in (
                self.qualifying_at,
                self.qualified_at,
                self.qualification_evidence_hash,
            )
        ):
            raise ValueError("registered candidate cannot have qualification evidence")
        if self.status is CandidateStatus.QUALIFYING and (
            self.qualifying_at is None
            or self.qualified_at is not None
            or self.qualification_evidence_hash is not None
        ):
            raise ValueError("qualifying candidate state is incomplete")
        if self.status in (CandidateStatus.QUALIFIED, CandidateStatus.REJECTED) and (
            self.qualifying_at is None
            or self.qualified_at is None
            or self.qualification_evidence_hash is None
        ):
            raise ValueError("terminal candidate requires complete qualification evidence")

    @classmethod
    def register(
        cls,
        descriptor: CandidateDescriptor,
        *,
        creator_deployment_id: str,
        registered_at: datetime,
    ) -> PlatformCandidate:
        return cls(
            descriptor=descriptor,
            status=CandidateStatus.REGISTERED,
            creator_deployment_id=creator_deployment_id,
            registered_at=registered_at,
            qualifying_at=None,
            qualified_at=None,
            qualification_evidence_hash=None,
        )

    def begin_qualification(self, qualifying_at: datetime) -> PlatformCandidate:
        self._require_nonterminal()
        if self.status is not CandidateStatus.REGISTERED:
            raise CandidateTransitionError("candidate is already qualifying")
        return replace(
            self,
            status=CandidateStatus.QUALIFYING,
            qualifying_at=qualifying_at,
        )

    def qualify(self, evidence_hash: str, qualified_at: datetime) -> PlatformCandidate:
        return self._finish(CandidateStatus.QUALIFIED, evidence_hash, qualified_at)

    def reject(self, evidence_hash: str, rejected_at: datetime) -> PlatformCandidate:
        return self._finish(CandidateStatus.REJECTED, evidence_hash, rejected_at)

    def _finish(
        self,
        status: CandidateStatus,
        evidence_hash: str,
        terminal_at: datetime,
    ) -> PlatformCandidate:
        self._require_nonterminal()
        if self.status is not CandidateStatus.QUALIFYING:
            raise CandidateTransitionError("candidate must be qualifying before terminal review")
        return replace(
            self,
            status=status,
            qualified_at=terminal_at,
            qualification_evidence_hash=evidence_hash,
        )

    def _require_nonterminal(self) -> None:
        if self.status in (CandidateStatus.QUALIFIED, CandidateStatus.REJECTED):
            raise CandidateTransitionError(f"candidate status {self.status.value} is terminal")


@dataclass(frozen=True, slots=True)
class PlatformRun:
    id: UUID
    experiment_id: UUID
    candidate_hash: str
    manifest_hash: str
    database_system_identifier: str
    railway_environment_id: str
    purpose: RunPurpose
    status: RunStatus
    requested_operator_command_id: UUID | None
    activating_worker_boot_id: UUID | None
    continuity_invalidated: bool
    started_at: datetime | None
    invalidated_at: datetime | None
    invalidation_reason: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        _uuid("id", self.id)
        _uuid("experiment_id", self.experiment_id)
        _hash("candidate_hash", self.candidate_hash)
        _hash("manifest_hash", self.manifest_hash)
        if (
            not self.database_system_identifier
            or len(self.database_system_identifier) > 32
            or not self.database_system_identifier.isascii()
            or not self.database_system_identifier.isdecimal()
        ):
            raise ValueError("database_system_identifier must be 1-32 ASCII decimal digits")
        _trimmed("railway_environment_id", self.railway_environment_id)
        if not isinstance(self.purpose, RunPurpose):
            raise ValueError("purpose must be a RunPurpose")
        if not isinstance(self.status, RunStatus):
            raise ValueError("status must be a RunStatus")
        if self.requested_operator_command_id is not None:
            _uuid("requested_operator_command_id", self.requested_operator_command_id)
        if self.activating_worker_boot_id is not None:
            _uuid("activating_worker_boot_id", self.activating_worker_boot_id)
        if type(self.continuity_invalidated) is not bool:
            raise ValueError("continuity_invalidated must be a boolean")
        _utc(self.created_at, "created_at")
        if self.started_at is not None:
            _utc(self.started_at, "started_at")
            if self.started_at < self.created_at:
                raise ValueError("started_at cannot precede created_at")
        if self.invalidated_at is not None:
            _utc(self.invalidated_at, "invalidated_at")
            floor = self.started_at or self.created_at
            if self.invalidated_at < floor:
                raise ValueError("invalidated_at cannot precede run start")
        self._validate_lifecycle()

    @classmethod
    def create(
        cls,
        *,
        run_id: UUID,
        experiment_id: UUID,
        candidate_hash: str,
        manifest_hash: str,
        database_system_identifier: str,
        railway_environment_id: str,
        purpose: RunPurpose,
        created_at: datetime,
    ) -> PlatformRun:
        return cls(
            id=run_id,
            experiment_id=experiment_id,
            candidate_hash=candidate_hash,
            manifest_hash=manifest_hash,
            database_system_identifier=database_system_identifier,
            railway_environment_id=railway_environment_id,
            purpose=purpose,
            status=RunStatus.STANDBY,
            requested_operator_command_id=None,
            activating_worker_boot_id=None,
            continuity_invalidated=False,
            started_at=None,
            invalidated_at=None,
            invalidation_reason=None,
            created_at=created_at,
        )

    def activate(
        self,
        *,
        command_id: UUID | None,
        worker_boot_id: UUID | None,
        started_at: datetime,
    ) -> PlatformRun:
        if self.status is RunStatus.INVALIDATED:
            raise RunTransitionError("invalidated run cannot be activated")
        if self.status is not RunStatus.STANDBY:
            raise RunTransitionError(f"run status {self.status.value} cannot be activated")
        if command_id is None:
            raise RunTransitionError("activation requires an operator command")
        if worker_boot_id is None:
            raise RunTransitionError("activation requires a worker boot")
        return replace(
            self,
            status=RunStatus.ACTIVE,
            requested_operator_command_id=command_id,
            activating_worker_boot_id=worker_boot_id,
            started_at=started_at,
        )

    def invalidate(self, reason: str, invalidated_at: datetime) -> PlatformRun:
        if self.status is RunStatus.INVALIDATED:
            if self.invalidation_reason == reason and self.invalidated_at == invalidated_at:
                return self
            raise RunTransitionError("invalidated run evidence is immutable")
        if self.status is RunStatus.COMPLETED:
            raise RunTransitionError("completed run cannot be invalidated")
        if self.status not in (RunStatus.STANDBY, RunStatus.ACTIVE):
            raise RunTransitionError(f"run status {self.status.value} cannot be invalidated")
        _trimmed("invalidation_reason", reason)
        return replace(
            self,
            status=RunStatus.INVALIDATED,
            continuity_invalidated=True,
            invalidated_at=invalidated_at,
            invalidation_reason=reason,
        )

    def complete(self) -> PlatformRun:
        if self.status is RunStatus.INVALIDATED:
            raise RunTransitionError("invalidated run cannot be completed")
        if self.status is not RunStatus.ACTIVE:
            raise RunTransitionError(f"run status {self.status.value} cannot be completed")
        return replace(self, status=RunStatus.COMPLETED)

    def _validate_lifecycle(self) -> None:
        if self.status is RunStatus.STANDBY:
            if (
                any(
                    value is not None
                    for value in (
                        self.requested_operator_command_id,
                        self.activating_worker_boot_id,
                        self.started_at,
                        self.invalidated_at,
                        self.invalidation_reason,
                    )
                )
                or self.continuity_invalidated
            ):
                raise ValueError("standby run lifecycle is invalid")
        elif self.status in (RunStatus.ACTIVE, RunStatus.COMPLETED):
            if (
                self.requested_operator_command_id is None
                or self.activating_worker_boot_id is None
                or self.started_at is None
                or self.continuity_invalidated
                or self.invalidated_at is not None
                or self.invalidation_reason is not None
            ):
                raise ValueError(f"{self.status.value} run lifecycle is invalid")
        elif (
            not self.continuity_invalidated
            or self.invalidated_at is None
            or self.invalidation_reason is None
            or (self.started_at is None) != (self.activating_worker_boot_id is None)
            or (self.started_at is None) != (self.requested_operator_command_id is None)
        ):
            raise ValueError("invalidated run lifecycle is incomplete")


@dataclass(frozen=True, slots=True)
class ServiceInstance:
    boot_id: UUID
    run_id: UUID | None
    identity: RailwayRuntimeIdentity
    first_seen_at: datetime
    last_heartbeat_at: datetime
    heartbeat_sequence: int
    stopped_at: datetime | None
    terminal_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RailwayRuntimeIdentity):
            raise ValueError("identity must be a RailwayRuntimeIdentity")
        if self.boot_id != self.identity.boot_id:
            raise ValueError("service boot_id must match runtime identity")
        if self.run_id is not None:
            _uuid("run_id", self.run_id)
        _utc(self.first_seen_at, "first_seen_at")
        _utc(self.last_heartbeat_at, "last_heartbeat_at")
        if self.first_seen_at < self.identity.started_at:
            raise ValueError("first_seen_at cannot precede process started_at")
        if self.last_heartbeat_at < self.first_seen_at:
            raise ValueError("last_heartbeat_at cannot precede first_seen_at")
        if type(self.heartbeat_sequence) is not int or self.heartbeat_sequence < 0:
            raise ValueError("heartbeat_sequence must be nonnegative")
        if (self.stopped_at is None) != (self.terminal_reason is None):
            raise ValueError("stopped_at and terminal_reason must be recorded together")
        if self.stopped_at is not None:
            _utc(self.stopped_at, "stopped_at")
            if self.stopped_at < self.last_heartbeat_at:
                raise ValueError("stopped_at cannot precede last heartbeat")
            assert self.terminal_reason is not None
            _trimmed("terminal_reason", self.terminal_reason)

    @classmethod
    def register(
        cls,
        identity: RailwayRuntimeIdentity,
        *,
        run_id: UUID | None,
        first_seen_at: datetime,
    ) -> ServiceInstance:
        return cls(
            boot_id=identity.boot_id,
            run_id=run_id,
            identity=identity,
            first_seen_at=first_seen_at,
            last_heartbeat_at=first_seen_at,
            heartbeat_sequence=0,
            stopped_at=None,
            terminal_reason=None,
        )

    def heartbeat(self, *, sequence: int, heartbeat_at: datetime) -> ServiceInstance:
        if self.stopped_at is not None:
            raise ServiceInstanceConflict("stopped service cannot heartbeat")
        if sequence == self.heartbeat_sequence and heartbeat_at == self.last_heartbeat_at:
            return self
        if sequence <= self.heartbeat_sequence:
            raise ServiceInstanceConflict("heartbeat sequence must increase")
        if heartbeat_at < self.last_heartbeat_at:
            raise ServiceInstanceConflict("heartbeat time cannot regress")
        return replace(
            self,
            last_heartbeat_at=heartbeat_at,
            heartbeat_sequence=sequence,
        )

    def stop(self, reason: str, stopped_at: datetime) -> ServiceInstance:
        if self.stopped_at is not None:
            if self.terminal_reason == reason and self.stopped_at == stopped_at:
                return self
            raise ServiceInstanceConflict("stopped service terminal evidence is immutable")
        _trimmed("terminal_reason", reason)
        return replace(self, stopped_at=stopped_at, terminal_reason=reason)


def _uuid(name: str, value: object) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{name} must be a non-nil UUID")


def _hash(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")


def _trimmed(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be nonempty and trimmed")


def _utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC-aware")
