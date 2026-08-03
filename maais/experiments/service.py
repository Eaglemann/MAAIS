from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from maais.domain.enums import ExperimentStatus
from maais.domain.events import NewDomainEvent
from maais.domain.json import JsonValue
from maais.experiments.manifest import ExperimentManifest


class InvalidExperimentTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExperimentTransition:
    status: ExperimentStatus
    expected_version: int
    events: tuple[NewDomainEvent, ...]


_ALLOWED: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.CREATED: frozenset({ExperimentStatus.RUNNING}),
    ExperimentStatus.RUNNING: frozenset(
        {
            ExperimentStatus.PAUSED,
            ExperimentStatus.STOPPED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
        }
    ),
    ExperimentStatus.PAUSED: frozenset(
        {
            ExperimentStatus.RUNNING,
            ExperimentStatus.STOPPED,
            ExperimentStatus.FAILED,
        }
    ),
    ExperimentStatus.STOPPED: frozenset(),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.FAILED: frozenset(),
}

_EVENT_NAMES: dict[ExperimentStatus, str] = {
    ExperimentStatus.RUNNING: "experiment.started",
    ExperimentStatus.PAUSED: "experiment.paused",
    ExperimentStatus.STOPPED: "experiment.stopped",
    ExperimentStatus.COMPLETED: "experiment.completed",
    ExperimentStatus.FAILED: "experiment.failed",
}


class ExperimentLifecycle:
    def __init__(
        self,
        manifest: ExperimentManifest,
        status: ExperimentStatus,
        version: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if version < 1:
            raise ValueError("existing experiment stream version must be at least one")
        self._manifest = manifest
        self._status = status
        self._version = version
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _transition(
        self,
        target: ExperimentStatus,
        *,
        failure_reason: str | None = None,
    ) -> ExperimentTransition:
        if target not in _ALLOWED[self._status]:
            raise InvalidExperimentTransition(f"cannot transition {self._status} to {target}")
        occurred_at = self._now()
        payload: dict[str, JsonValue] = {
            "previous_status": self._status.value,
            "status": target.value,
            "config_hash": self._manifest.config_hash,
            "manifest_hash": self._manifest.manifest_hash,
        }
        if failure_reason is not None:
            payload["failure_reason"] = failure_reason
        event = NewDomainEvent(
            aggregate_id=self._manifest.experiment_id,
            aggregate_type="experiment",
            event_type=_EVENT_NAMES[target],
            payload=payload,
            metadata={"manifest_schema_version": self._manifest.manifest_schema_version},
            occurred_at=occurred_at,
        )
        return ExperimentTransition(target, self._version, (event,))

    def start(self) -> ExperimentTransition:
        return self._transition(ExperimentStatus.RUNNING)

    def pause(self) -> ExperimentTransition:
        return self._transition(ExperimentStatus.PAUSED)

    def resume(self) -> ExperimentTransition:
        return self._transition(ExperimentStatus.RUNNING)

    def stop(self) -> ExperimentTransition:
        return self._transition(ExperimentStatus.STOPPED)

    def complete(self) -> ExperimentTransition:
        return self._transition(ExperimentStatus.COMPLETED)

    def fail(self, reason: str) -> ExperimentTransition:
        if not reason.strip():
            raise ValueError("failure reason cannot be empty")
        return self._transition(ExperimentStatus.FAILED, failure_reason=reason.strip())
