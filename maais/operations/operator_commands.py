"""Immutable, idempotent operator-command lifecycle for the local paper platform."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from maais.domain.json import JsonValue, content_hash, freeze_json


class CommandType(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    EMERGENCY_HALT = "emergency_halt"
    FLATTEN = "flatten"
    ACKNOWLEDGE_INCIDENT = "acknowledge_incident"
    RESOLVE_INCIDENT = "resolve_incident"
    RESET_KILL_SWITCH = "reset_kill_switch"


class CommandStatus(StrEnum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REJECTED = "rejected"


_SAFETY_CRITICAL = frozenset(
    {
        CommandType.START,
        CommandType.PAUSE,
        CommandType.RESUME,
        CommandType.STOP,
        CommandType.EMERGENCY_HALT,
        CommandType.FLATTEN,
        CommandType.RESOLVE_INCIDENT,
        CommandType.RESET_KILL_SWITCH,
    }
)


def confirmation_phrase(command_type: CommandType) -> str:
    return f"CONFIRM {command_type.value.upper()}"


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


def _object(value: object, field: str) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{field} must be a JSON object")
    return frozen


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    command_id: UUID
    experiment_id: UUID
    command_type: CommandType
    status: CommandStatus
    idempotency_key: str
    actor: str
    reason: str
    payload: Mapping[str, JsonValue]
    operator_confirmed: bool
    request_hash: str
    requested_at: datetime
    version: int
    accepted_at: datetime | None = None
    accepted_by: str | None = None
    completed_at: datetime | None = None
    result: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.command_id.int == 0 or self.experiment_id.int == 0:
            raise ValueError("operator command identities cannot be nil")
        if (
            not self.idempotency_key
            or self.idempotency_key != self.idempotency_key.strip()
            or not 8 <= len(self.idempotency_key) <= 128
        ):
            raise ValueError("idempotency key must be 8-128 trimmed characters")
        if not self.actor or self.actor != self.actor.strip():
            raise ValueError("operator command actor must be nonempty and trimmed")
        if not self.reason or self.reason != self.reason.strip():
            raise ValueError("operator command reason must be nonempty and trimmed")
        if len(self.request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_hash
        ):
            raise ValueError("operator command request hash must be a lowercase SHA-256 digest")
        if self.version <= 0:
            raise ValueError("operator command version must be positive")
        _require_utc(self.requested_at, "requested_at")
        if self.accepted_at is not None:
            _require_utc(self.accepted_at, "accepted_at")
        if self.completed_at is not None:
            _require_utc(self.completed_at, "completed_at")
        if self.accepted_at is not None and self.accepted_at < self.requested_at:
            raise ValueError("operator command acceptance time cannot regress")
        if self.completed_at is not None and (
            self.accepted_at is None or self.completed_at < self.accepted_at
        ):
            raise ValueError("operator command completion time cannot regress")
        if self.command_type in _SAFETY_CRITICAL and not self.operator_confirmed:
            raise ValueError("safety-critical operator command must be confirmed")
        minimum_version, has_acceptance, has_completion = {
            CommandStatus.REQUESTED: (1, False, False),
            CommandStatus.ACCEPTED: (2, True, False),
            CommandStatus.COMPLETED: (3, True, True),
            CommandStatus.REJECTED: (3, True, True),
        }[self.status]
        if (
            self.version != minimum_version
            if self.status is CommandStatus.REQUESTED
            else self.version < minimum_version
        ):
            raise ValueError("operator command version differs from lifecycle status")
        acceptance_complete = self.accepted_at is not None and self.accepted_by is not None
        acceptance_empty = self.accepted_at is None and self.accepted_by is None
        if (has_acceptance and not acceptance_complete) or (
            not has_acceptance and not acceptance_empty
        ):
            raise ValueError("operator command acceptance shape is invalid")
        completion_complete = self.completed_at is not None and self.result is not None
        completion_empty = self.completed_at is None and self.result is None
        if (has_completion and not completion_complete) or (
            not has_completion and not completion_empty
        ):
            raise ValueError("operator command completion shape is invalid")
        if self.accepted_by is not None and (
            not self.accepted_by or self.accepted_by != self.accepted_by.strip()
        ):
            raise ValueError("operator command worker identity must be nonempty and trimmed")
        object.__setattr__(self, "payload", _object(self.payload, "payload"))
        if self.result is not None:
            object.__setattr__(self, "result", _object(self.result, "result"))

    @classmethod
    def request(
        cls,
        *,
        command_id: UUID,
        experiment_id: UUID,
        command_type: CommandType,
        idempotency_key: str,
        actor: str,
        reason: str,
        payload: Mapping[str, object],
        confirmation: str | None,
        requested_at: datetime,
    ) -> OperatorCommand:
        required_confirmation = confirmation_phrase(command_type)
        if command_type in _SAFETY_CRITICAL and confirmation != required_confirmation:
            raise ValueError(f"command requires exact confirmation: {required_confirmation}")
        operator_confirmed = confirmation == required_confirmation
        normalized_payload = _object(payload, "payload")
        request_identity = {
            "experiment_id": experiment_id,
            "command_type": command_type,
            "idempotency_key": idempotency_key,
            "actor": actor,
            "reason": reason,
            "payload": normalized_payload,
            "operator_confirmed": operator_confirmed,
        }
        return cls(
            command_id=command_id,
            experiment_id=experiment_id,
            command_type=command_type,
            status=CommandStatus.REQUESTED,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
            payload=normalized_payload,
            operator_confirmed=operator_confirmed,
            request_hash=content_hash(request_identity),
            requested_at=requested_at,
            version=1,
        )

    def accept(self, *, accepted_at: datetime, worker_id: str) -> OperatorCommand:
        if self.status is CommandStatus.ACCEPTED:
            if self.accepted_by == worker_id:
                return self
            raise ValueError("operator command was accepted by a different worker")
        if self.status is not CommandStatus.REQUESTED:
            raise ValueError("only a requested operator command can be accepted")
        return replace(
            self,
            status=CommandStatus.ACCEPTED,
            version=2,
            accepted_at=accepted_at,
            accepted_by=worker_id,
        )

    def take_over(self, *, worker_id: str) -> OperatorCommand:
        if self.status is not CommandStatus.ACCEPTED:
            raise ValueError("only an accepted operator command can be taken over")
        if not worker_id or worker_id != worker_id.strip():
            raise ValueError("operator command worker identity must be nonempty and trimmed")
        if self.accepted_by == worker_id:
            return self
        return replace(
            self,
            version=self.version + 1,
            accepted_by=worker_id,
        )

    def complete(
        self,
        *,
        completed_at: datetime,
        result: Mapping[str, object],
    ) -> OperatorCommand:
        normalized_result = _object(result, "result")
        if self.status is CommandStatus.COMPLETED:
            if self.result == normalized_result:
                return self
            raise ValueError("operator command is already completed with a different result")
        if self.status is not CommandStatus.ACCEPTED:
            raise ValueError("only an accepted operator command can be completed")
        return replace(
            self,
            status=CommandStatus.COMPLETED,
            version=self.version + 1,
            completed_at=completed_at,
            result=normalized_result,
        )

    def reject(
        self,
        *,
        completed_at: datetime,
        reason_code: str,
        detail: str,
    ) -> OperatorCommand:
        if not reason_code or reason_code != reason_code.strip():
            raise ValueError("command rejection reason code must be nonempty and trimmed")
        if not detail or detail != detail.strip():
            raise ValueError("command rejection detail must be nonempty and trimmed")
        result = {"reason_code": reason_code, "detail": detail}
        if self.status is CommandStatus.REJECTED:
            if self.result == _object(result, "result"):
                return self
            raise ValueError("operator command is already rejected with a different result")
        if self.status is not CommandStatus.ACCEPTED:
            raise ValueError("only an accepted operator command can be rejected")
        return replace(
            self,
            status=CommandStatus.REJECTED,
            version=self.version + 1,
            completed_at=completed_at,
            result=result,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "experiment_id": self.experiment_id,
            "command_type": self.command_type,
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "actor": self.actor,
            "reason": self.reason,
            "payload": self.payload,
            "operator_confirmed": self.operator_confirmed,
            "request_hash": self.request_hash,
            "requested_at": self.requested_at,
            "version": self.version,
            "accepted_at": self.accepted_at,
            "accepted_by": self.accepted_by,
            "completed_at": self.completed_at,
            "result": self.result,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())
