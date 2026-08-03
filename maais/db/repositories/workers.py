"""Atomic PostgreSQL worker ownership leases with audited lifecycle events."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.operations import WorkerLeaseModel
from maais.db.repositories.events import EventRepository
from maais.db.repositories.market_data import _new_event
from maais.execution.paper.clock import require_utc
from maais.orchestration.checkpoints import WorkerLease, WorkerLeaseStatus


class WorkerLeaseConflict(RuntimeError):
    pass


class WorkerLeaseExpired(WorkerLeaseConflict):
    pass


class WorkerLeaseRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def acquire(
        self,
        *,
        experiment_id: UUID,
        worker_id: UUID,
        acquired_at: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        _validate_request(experiment_id, worker_id, acquired_at, ttl)
        expires_at = acquired_at + ttl
        created_id = await self._session.scalar(
            insert(WorkerLeaseModel)
            .values(
                experiment_id=experiment_id,
                worker_id=worker_id,
                status=WorkerLeaseStatus.ACTIVE.value,
                acquired_at=acquired_at,
                heartbeat_at=acquired_at,
                expires_at=expires_at,
                released_at=None,
                epoch=1,
            )
            .on_conflict_do_nothing(index_elements=[WorkerLeaseModel.experiment_id])
            .returning(WorkerLeaseModel.experiment_id)
        )
        if created_id is not None:
            lease = WorkerLease(
                experiment_id,
                worker_id,
                WorkerLeaseStatus.ACTIVE,
                acquired_at,
                acquired_at,
                expires_at,
                None,
                1,
            )
            await self._append_transition(lease, "worker_lease.acquired", previous=None)
            return lease

        row = await self._locked(experiment_id)
        existing = _from_row(row)
        if existing.active and existing.valid_at(acquired_at):
            if existing.worker_id == worker_id:
                return existing
            raise WorkerLeaseConflict(
                "experiment lease is held by another worker until "
                f"{existing.expires_at.isoformat()}"
            )

        previous = existing
        row.worker_id = worker_id
        row.status = WorkerLeaseStatus.ACTIVE.value
        row.acquired_at = acquired_at
        row.heartbeat_at = acquired_at
        row.expires_at = expires_at
        row.released_at = None
        row.epoch = existing.epoch + 1
        lease = _from_row(row)
        event_type = (
            "worker_lease.taken_over"
            if previous.active and previous.worker_id != worker_id
            else "worker_lease.reacquired"
        )
        await self._append_transition(lease, event_type, previous=previous)
        return lease

    async def renew(
        self,
        *,
        experiment_id: UUID,
        worker_id: UUID,
        heartbeat_at: datetime,
        ttl: timedelta,
    ) -> WorkerLease:
        _validate_request(experiment_id, worker_id, heartbeat_at, ttl)
        row = await self._locked(experiment_id)
        current = _from_row(row)
        if not current.active or current.worker_id != worker_id:
            raise WorkerLeaseConflict("worker does not own the active experiment lease")
        if not current.valid_at(heartbeat_at):
            raise WorkerLeaseExpired("worker lease expired before heartbeat renewal")
        if heartbeat_at < current.heartbeat_at:
            raise WorkerLeaseConflict("worker lease heartbeat cannot regress")
        row.heartbeat_at = heartbeat_at
        row.expires_at = heartbeat_at + ttl
        return _from_row(row)

    async def release(
        self,
        *,
        experiment_id: UUID,
        worker_id: UUID,
        released_at: datetime,
    ) -> WorkerLease:
        require_utc(released_at, "worker lease released_at")
        row = await self._locked(experiment_id)
        current = _from_row(row)
        if current.worker_id != worker_id:
            raise WorkerLeaseConflict("worker cannot release another worker's lease")
        if not current.active:
            return current
        if released_at < current.heartbeat_at:
            raise WorkerLeaseConflict("worker lease release cannot precede its heartbeat")
        row.status = WorkerLeaseStatus.RELEASED.value
        row.expires_at = released_at
        row.released_at = released_at
        released = _from_row(row)
        await self._append_transition(released, "worker_lease.released", previous=current)
        return released

    async def get(self, experiment_id: UUID, *, for_update: bool = False) -> WorkerLease:
        statement = select(WorkerLeaseModel).where(WorkerLeaseModel.experiment_id == experiment_id)
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise LookupError("worker lease does not exist")
        return _from_row(row)

    async def _locked(self, experiment_id: UUID) -> WorkerLeaseModel:
        row = await self._session.scalar(
            select(WorkerLeaseModel)
            .where(WorkerLeaseModel.experiment_id == experiment_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("worker lease does not exist")
        return row

    async def _append_transition(
        self,
        lease: WorkerLease,
        event_type: str,
        *,
        previous: WorkerLease | None,
    ) -> None:
        occurred_at = lease.acquired_at
        if lease.status is WorkerLeaseStatus.RELEASED:
            assert lease.released_at is not None
            occurred_at = lease.released_at
        expected = await self._events.stream_version(lease.experiment_id, "worker_lease")
        await self._events.append(
            lease.experiment_id,
            "worker_lease",
            expected,
            (
                _new_event(
                    aggregate_id=lease.experiment_id,
                    aggregate_type="worker_lease",
                    event_type=event_type,
                    payload={
                        "experiment_id": lease.experiment_id,
                        "worker_id": lease.worker_id,
                        "status": lease.status,
                        "acquired_at": lease.acquired_at,
                        "heartbeat_at": lease.heartbeat_at,
                        "expires_at": lease.expires_at,
                        "released_at": lease.released_at,
                        "epoch": lease.epoch,
                        "previous_worker_id": (
                            previous.worker_id if previous is not None else None
                        ),
                        "previous_epoch": previous.epoch if previous is not None else None,
                    },
                    occurred_at=occurred_at,
                ),
            ),
        )


def _validate_request(
    experiment_id: UUID,
    worker_id: UUID,
    observed_at: datetime,
    ttl: timedelta,
) -> None:
    if experiment_id.int == 0 or worker_id.int == 0:
        raise ValueError("worker lease UUIDs cannot be nil")
    require_utc(observed_at, "worker lease observed_at")
    if ttl <= timedelta(0):
        raise ValueError("worker lease TTL must be positive")


def _from_row(row: WorkerLeaseModel) -> WorkerLease:
    return WorkerLease(
        experiment_id=row.experiment_id,
        worker_id=row.worker_id,
        status=WorkerLeaseStatus(row.status),
        acquired_at=row.acquired_at,
        heartbeat_at=row.heartbeat_at,
        expires_at=row.expires_at,
        released_at=row.released_at,
        epoch=row.epoch,
    )
