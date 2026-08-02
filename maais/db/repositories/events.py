from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.ledger import DomainEventModel, EventStreamModel, OutboxEventModel
from maais.domain.events import NewDomainEvent, StoredDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, freeze_json, to_json_data


class OptimisticConcurrencyError(RuntimeError):
    def __init__(
        self,
        aggregate_id: UUID,
        aggregate_type: str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.aggregate_id = aggregate_id
        self.aggregate_type = aggregate_type
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"stream {aggregate_type}/{aggregate_id} expected version "
            f"{expected_version}, actual {actual_version}"
        )


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):
        raise TypeError("expected a JSON object")
    return normalized


def _event_object(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("expected an immutable JSON object")
    return normalized


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        aggregate_id: UUID,
        aggregate_type: str,
        expected_version: int,
        events: Sequence[NewDomainEvent],
    ) -> tuple[StoredDomainEvent, ...]:
        if aggregate_id.int == 0:
            raise ValueError("aggregate_id cannot be nil")
        if not aggregate_type.strip():
            raise ValueError("aggregate_type cannot be empty")
        if expected_version < 0:
            raise ValueError("expected_version cannot be negative")
        if not events:
            raise ValueError("at least one event is required")
        for event in events:
            if event.aggregate_id != aggregate_id or event.aggregate_type != aggregate_type:
                raise ValueError("event aggregate identity does not match append target")

        await self._session.execute(
            insert(EventStreamModel)
            .values(
                aggregate_id=aggregate_id,
                aggregate_type=aggregate_type,
                current_version=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    EventStreamModel.aggregate_type,
                    EventStreamModel.aggregate_id,
                ]
            )
        )
        stream = await self._session.scalar(
            select(EventStreamModel)
            .where(
                EventStreamModel.aggregate_id == aggregate_id,
                EventStreamModel.aggregate_type == aggregate_type,
            )
            .with_for_update()
        )
        if stream is None:
            raise RuntimeError("event stream row was not available after insert")
        if stream.current_version != expected_version:
            raise OptimisticConcurrencyError(
                aggregate_id,
                aggregate_type,
                expected_version,
                stream.current_version,
            )

        stored_events: list[StoredDomainEvent] = []
        next_version = expected_version
        for event in events:
            next_version += 1
            model = DomainEventModel(
                stream_id=stream.id,
                aggregate_id=aggregate_id,
                aggregate_type=aggregate_type,
                stream_version=next_version,
                event_type=event.event_type,
                event_version=event.event_version,
                payload_json=_json_object(event.payload),
                metadata_json=_json_object(event.metadata),
                occurred_at=event.occurred_at,
            )
            self._session.add(model)
            await self._session.flush()
            stored = StoredDomainEvent(
                id=model.id,
                global_position=model.global_position,
                stream_version=next_version,
                aggregate_id=aggregate_id,
                aggregate_type=aggregate_type,
                event_type=event.event_type,
                payload=event.payload,
                metadata=event.metadata,
                occurred_at=event.occurred_at,
                event_version=event.event_version,
            )
            stored_events.append(stored)
            self._session.add(
                OutboxEventModel(
                    domain_event_id=model.id,
                    topic=event.event_type,
                    payload_json=_json_object(
                        {
                            "event_id": model.id,
                            "global_position": model.global_position,
                            "aggregate_id": aggregate_id,
                            "aggregate_type": aggregate_type,
                            "stream_version": next_version,
                            "event_type": event.event_type,
                            "event_version": event.event_version,
                            "occurred_at": event.occurred_at,
                            "payload": event.payload,
                            "metadata": event.metadata,
                        }
                    ),
                    publish_attempts=0,
                )
            )

        stream.current_version = next_version
        stream.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        return tuple(stored_events)

    async def load_stream(
        self,
        aggregate_id: UUID,
        aggregate_type: str,
        after_version: int = 0,
    ) -> tuple[StoredDomainEvent, ...]:
        if after_version < 0:
            raise ValueError("after_version cannot be negative")
        rows = (
            await self._session.scalars(
                select(DomainEventModel)
                .where(
                    DomainEventModel.aggregate_id == aggregate_id,
                    DomainEventModel.aggregate_type == aggregate_type,
                    DomainEventModel.stream_version > after_version,
                )
                .order_by(DomainEventModel.stream_version)
            )
        ).all()
        return tuple(
            StoredDomainEvent(
                id=row.id,
                global_position=row.global_position,
                stream_version=row.stream_version,
                aggregate_id=row.aggregate_id,
                aggregate_type=row.aggregate_type,
                event_type=row.event_type,
                event_version=row.event_version,
                payload=_event_object(row.payload_json),
                metadata=_event_object(row.metadata_json),
                occurred_at=row.occurred_at,
            )
            for row in rows
        )

    async def stream_version(self, aggregate_id: UUID, aggregate_type: str) -> int:
        result = await self._session.scalar(
            select(EventStreamModel.current_version).where(
                EventStreamModel.aggregate_id == aggregate_id,
                EventStreamModel.aggregate_type == aggregate_type,
            )
        )
        return int(result or 0)

    async def unpublished_outbox_count(self) -> int:
        result = await self._session.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .where(OutboxEventModel.published_at.is_(None))
        )
        return int(result or 0)
