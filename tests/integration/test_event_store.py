from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.repositories.events import OptimisticConcurrencyError
from maais.db.unit_of_work import UnitOfWork
from maais.domain.events import NewDomainEvent

pytestmark = pytest.mark.integration

AGGREGATE_ID = UUID("22222222-2222-4222-8222-222222222222")


def _event(aggregate_id: UUID = AGGREGATE_ID, sequence: int = 1) -> NewDomainEvent:
    return NewDomainEvent(
        aggregate_id=aggregate_id,
        aggregate_type="experiment",
        event_type="experiment.observed",
        payload={"sequence": sequence},
        metadata={"correlation_id": str(uuid4())},
        occurred_at=datetime.now(timezone.utc),
    )


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    factory = async_sessionmaker(engine)
    async with factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(DomainEventModel))
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEventModel))
    return int(event_count or 0), int(outbox_count or 0)


async def test_append_assigns_gapless_versions_and_outbox(
    uow_factory: UnitOfWork,
) -> None:
    async with uow_factory.begin() as uow:
        stored = await uow.events.append(
            AGGREGATE_ID,
            "experiment",
            0,
            (_event(sequence=1), _event(sequence=2)),
        )

    assert [event.stream_version for event in stored] == [1, 2]
    assert stored[0].global_position < stored[1].global_position
    async with uow_factory.begin() as uow:
        assert await uow.events.stream_version(AGGREGATE_ID, "experiment") == 2
        assert await uow.events.unpublished_outbox_count() == 2
        loaded = await uow.events.load_stream(AGGREGATE_ID, "experiment")
    assert loaded == stored


async def test_stale_expected_version_changes_nothing(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    async with uow_factory.begin() as uow:
        await uow.events.append(AGGREGATE_ID, "experiment", 0, (_event(),))

    with pytest.raises(OptimisticConcurrencyError):
        async with uow_factory.begin() as uow:
            await uow.events.append(AGGREGATE_ID, "experiment", 0, (_event(sequence=2),))

    assert await _counts(db_engine) == (1, 1)


async def test_unit_of_work_rolls_back_event_and_outbox_on_exception(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    with pytest.raises(RuntimeError, match="forced rollback"):
        async with uow_factory.begin() as uow:
            await uow.events.append(AGGREGATE_ID, "experiment", 0, (_event(),))
            raise RuntimeError("forced rollback")

    assert await _counts(db_engine) == (0, 0)


async def test_same_stream_concurrency_has_one_winner(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    ready = asyncio.Event()
    entered = 0
    lock = asyncio.Lock()

    async def append_once(sequence: int) -> str:
        nonlocal entered
        async with lock:
            entered += 1
            if entered == 2:
                ready.set()
        await ready.wait()
        try:
            async with uow_factory.begin() as uow:
                await uow.events.append(
                    AGGREGATE_ID,
                    "experiment",
                    0,
                    (_event(sequence=sequence),),
                )
            return "stored"
        except OptimisticConcurrencyError:
            return "conflict"

    results = await asyncio.gather(append_once(1), append_once(2))

    assert sorted(results) == ["conflict", "stored"]
    assert await _counts(db_engine) == (1, 1)


async def test_different_streams_can_append_concurrently(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    aggregate_ids = (uuid4(), uuid4())

    async def append(aggregate_id: UUID) -> None:
        async with uow_factory.begin() as uow:
            await uow.events.append(
                aggregate_id,
                "experiment",
                0,
                (_event(aggregate_id),),
            )

    await asyncio.gather(*(append(aggregate_id) for aggregate_id in aggregate_ids))

    assert await _counts(db_engine) == (2, 2)
