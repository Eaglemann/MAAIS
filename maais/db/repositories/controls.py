"""Persistent halt-only trading controls."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import TradingControlModel
from maais.db.repositories.events import EventRepository
from maais.db.repositories.market_data import _json_object, _new_event
from maais.domain.json import content_hash
from maais.operations.controls import TradingControlSnapshot


class TradingControlConflict(RuntimeError):
    pass


class TradingControlRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def initialize(
        self,
        experiment_id: UUID,
        *,
        initialized_at: datetime,
        actor: str = "system",
    ) -> TradingControlSnapshot:
        candidate = TradingControlSnapshot.initialize(
            experiment_id,
            initialized_at=initialized_at,
            actor=actor,
        )
        state = _json_object(candidate.to_dict())
        state_hash = content_hash(state)
        created = await self._session.scalar(
            insert(TradingControlModel)
            .values(
                experiment_id=experiment_id,
                kill_switch_active=False,
                reason=None,
                version=candidate.version,
                changed_at=candidate.changed_at,
                changed_by=candidate.changed_by,
                state_json=state,
                content_hash=state_hash,
            )
            .on_conflict_do_nothing(index_elements=[TradingControlModel.experiment_id])
            .returning(TradingControlModel.experiment_id)
        )
        if created is None:
            return await self.current(experiment_id)
        await self._events.append(
            experiment_id,
            "trading_control",
            0,
            (
                _new_event(
                    aggregate_id=experiment_id,
                    aggregate_type="trading_control",
                    event_type="trading_control.initialized",
                    payload=state,
                    occurred_at=initialized_at,
                ),
            ),
        )
        return candidate

    async def halt(
        self,
        experiment_id: UUID,
        *,
        reason: str,
        halted_at: datetime,
        actor: str,
    ) -> TradingControlSnapshot:
        row = await self._locked(experiment_id)
        current = _from_row(row)
        if content_hash(_json_object(current.to_dict())) != row.content_hash:
            raise TradingControlConflict("trading control content hash is invalid")
        halted = current.halt(reason, halted_at=halted_at, actor=actor)
        if halted == current:
            return current
        state = _json_object(halted.to_dict())
        row.kill_switch_active = halted.kill_switch_active
        row.reason = halted.reason
        row.version = halted.version
        row.changed_at = halted.changed_at
        row.changed_by = halted.changed_by
        row.state_json = state
        row.content_hash = content_hash(state)
        await self._events.append(
            experiment_id,
            "trading_control",
            current.version,
            (
                _new_event(
                    aggregate_id=experiment_id,
                    aggregate_type="trading_control",
                    event_type="trading_control.halted",
                    payload=state,
                    occurred_at=halted_at,
                ),
            ),
        )
        return halted

    async def current(self, experiment_id: UUID) -> TradingControlSnapshot:
        row = await self._session.get(TradingControlModel, experiment_id)
        if row is None:
            raise LookupError("trading controls are not initialized")
        snapshot = _from_row(row)
        if content_hash(_json_object(snapshot.to_dict())) != row.content_hash:
            raise TradingControlConflict("trading control content hash is invalid")
        return snapshot

    async def reset(
        self,
        experiment_id: UUID,
        *,
        reset_at: datetime,
        actor: str,
        expected_version: int,
        allowed_reason_prefix: str,
    ) -> TradingControlSnapshot:
        row = await self._locked(experiment_id)
        current = _from_row(row)
        if content_hash(_json_object(current.to_dict())) != row.content_hash:
            raise TradingControlConflict("trading control content hash is invalid")
        reset = current.reset(
            reset_at=reset_at,
            actor=actor,
            expected_version=expected_version,
            allowed_reason_prefix=allowed_reason_prefix,
        )
        if reset == current:
            return current
        state = _json_object(reset.to_dict())
        row.kill_switch_active = reset.kill_switch_active
        row.reason = reset.reason
        row.version = reset.version
        row.changed_at = reset.changed_at
        row.changed_by = reset.changed_by
        row.state_json = state
        row.content_hash = content_hash(state)
        await self._events.append(
            experiment_id,
            "trading_control",
            current.version,
            (
                _new_event(
                    aggregate_id=experiment_id,
                    aggregate_type="trading_control",
                    event_type="trading_control.reset",
                    payload=state,
                    occurred_at=reset_at,
                ),
            ),
        )
        return reset

    async def _locked(self, experiment_id: UUID) -> TradingControlModel:
        row = await self._session.scalar(
            select(TradingControlModel)
            .where(TradingControlModel.experiment_id == experiment_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("trading controls are not initialized")
        return row


def _from_row(row: TradingControlModel) -> TradingControlSnapshot:
    return TradingControlSnapshot(
        experiment_id=row.experiment_id,
        kill_switch_active=row.kill_switch_active,
        reason=row.reason,
        version=row.version,
        changed_at=row.changed_at,
        changed_by=row.changed_by,
    )
