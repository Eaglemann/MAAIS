"""Persistent trading-control domain state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from maais.execution.paper.clock import require_utc


@dataclass(frozen=True, slots=True)
class TradingControlSnapshot:
    experiment_id: UUID
    kill_switch_active: bool
    reason: str | None
    version: int
    changed_at: datetime
    changed_by: str

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or self.version <= 0:
            raise ValueError("trading control identity is invalid")
        require_utc(self.changed_at, "trading control changed_at")
        if not self.changed_by:
            raise ValueError("trading control actor is required")
        if self.kill_switch_active != (self.reason is not None):
            raise ValueError("trading control state and reason must appear together")

    @classmethod
    def initialize(
        cls,
        experiment_id: UUID,
        *,
        initialized_at: datetime,
        actor: str,
    ) -> TradingControlSnapshot:
        return cls(experiment_id, False, None, 1, initialized_at, actor)

    def halt(
        self,
        reason: str,
        *,
        halted_at: datetime,
        actor: str,
    ) -> TradingControlSnapshot:
        require_utc(halted_at, "trading control halted_at")
        if not reason or not actor:
            raise ValueError("trading halt requires a reason and actor")
        if halted_at < self.changed_at:
            raise ValueError("trading control time cannot regress")
        if self.kill_switch_active and self.reason == reason:
            return self
        return replace(
            self,
            kill_switch_active=True,
            reason=reason,
            version=self.version + 1,
            changed_at=halted_at,
            changed_by=actor,
        )

    def reset(
        self,
        *,
        reset_at: datetime,
        actor: str,
        expected_version: int,
        allowed_reason_prefix: str,
    ) -> TradingControlSnapshot:
        require_utc(reset_at, "trading control reset_at")
        if not actor or not allowed_reason_prefix:
            raise ValueError("trading reset requires an actor and allowed reason prefix")
        if expected_version != self.version:
            raise ValueError("trading control version changed before reset")
        if reset_at < self.changed_at:
            raise ValueError("trading control time cannot regress")
        if not self.kill_switch_active:
            return self
        assert self.reason is not None
        if not self.reason.startswith(allowed_reason_prefix):
            raise ValueError("trading control reason is not eligible for this reset")
        return replace(
            self,
            kill_switch_active=False,
            reason=None,
            version=self.version + 1,
            changed_at=reset_at,
            changed_by=actor,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "kill_switch_active": self.kill_switch_active,
            "reason": self.reason,
            "version": self.version,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
        }
