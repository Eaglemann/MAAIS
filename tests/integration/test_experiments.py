from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from maais.config.constants import ALL_AGENTS
from maais.db.models.experiments import AgentVersionModel, ExperimentModel
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.repositories.events import OptimisticConcurrencyError
from maais.db.repositories.experiments import ImmutableManifestError
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ExperimentStatus
from maais.experiments.service import ExperimentLifecycle
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration


async def _table_names(connection: AsyncConnection) -> set[str]:
    names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    return set(names)


async def _constraint_exists(connection: AsyncConnection, name: str) -> bool:
    def find(sync_connection: object) -> bool:
        inspector = inspect(sync_connection)
        for table in ("domain_events", "outbox_events"):
            if any(item.get("name") == name for item in inspector.get_unique_constraints(table)):
                return True
        return False

    return await connection.run_sync(find)


async def test_event_and_experiment_schema_contract(db_connection: AsyncConnection) -> None:
    tables = await _table_names(db_connection)
    assert {
        "event_streams",
        "domain_events",
        "outbox_events",
        "experiments",
        "strategy_versions",
        "agent_versions",
    } <= tables
    assert await _constraint_exists(db_connection, "uq_domain_event_stream_version")
    assert await _constraint_exists(db_connection, "uq_outbox_domain_event")


async def test_domain_event_update_and_delete_are_blocked(db_engine: AsyncEngine) -> None:
    stream_id = uuid4()
    event_id = uuid4()
    aggregate_id = uuid4()
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO event_streams "
                "(id, aggregate_id, aggregate_type, current_version) "
                "VALUES (:id, :aggregate_id, 'experiment', 1)"
            ),
            {"id": stream_id, "aggregate_id": aggregate_id},
        )
        await connection.execute(
            text(
                "INSERT INTO domain_events "
                "(id, stream_id, aggregate_id, aggregate_type, stream_version, event_type, "
                "event_version, payload_json, metadata_json, occurred_at) "
                "VALUES (:id, :stream_id, :aggregate_id, 'experiment', 1, "
                "'experiment.created', 1, '{}'::jsonb, '{}'::jsonb, :occurred_at)"
            ),
            {
                "id": event_id,
                "stream_id": stream_id,
                "aggregate_id": aggregate_id,
                "occurred_at": datetime.now(timezone.utc),
            },
        )

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_engine.begin() as connection:
            await connection.execute(
                text("UPDATE domain_events SET event_type='changed' WHERE id=:id"),
                {"id": event_id},
            )

    with pytest.raises(DBAPIError, match="append-only"):
        async with db_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM domain_events WHERE id=:id"),
                {"id": event_id},
            )


async def test_create_manifest_projection_and_event_are_atomic(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = _manifest(schema_revision="0005")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.get(ExperimentModel, manifest.experiment_id)
        assert row is not None
        assert row.config_hash == manifest.config_hash
        assert row.manifest_hash == manifest.manifest_hash
        assert row.manifest_json == manifest.to_dict()
        assert await session.scalar(select(func.count()).select_from(AgentVersionModel)) == 8
        assert await session.scalar(select(func.count()).select_from(DomainEventModel)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxEventModel)) == 1

    async with uow_factory.begin() as uow:
        restored = await uow.experiments.get_manifest(manifest.experiment_id)
    assert restored == manifest


async def test_manifest_restores_exact_registered_agent_version_ids(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(schema_revision="0012")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        version_ids = await uow.experiments.get_agent_version_ids(manifest)

    assert tuple(version_ids) == ALL_AGENTS
    assert all(version_id.int != 0 for version_id in version_ids.values())


async def test_lifecycle_transition_does_not_mutate_manifest(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = _manifest(schema_revision="0005")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    lifecycle = ExperimentLifecycle(
        manifest,
        status=ExperimentStatus.CREATED,
        version=1,
        now=lambda: datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.transition(manifest, lifecycle.start())

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        row = await session.get(ExperimentModel, manifest.experiment_id)
        assert row is not None
        assert row.status == ExperimentStatus.RUNNING.value
        assert row.started_at == datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
        assert row.manifest_json == manifest.to_dict()


async def test_event_conflict_rolls_back_projection_transition(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    manifest = _manifest(schema_revision="0005")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    lifecycle = ExperimentLifecycle(
        manifest,
        ExperimentStatus.CREATED,
        version=1,
        now=lambda: datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
    )
    stale_transition = replace(lifecycle.start(), expected_version=0)

    with pytest.raises(OptimisticConcurrencyError):
        async with uow_factory.begin() as uow:
            await uow.experiments.transition(manifest, stale_transition)

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        row = await session.get(ExperimentModel, manifest.experiment_id)
        assert row is not None
        assert row.status == ExperimentStatus.CREATED.value
        assert row.started_at is None


async def test_transition_rejects_changed_manifest_identity(uow_factory: UnitOfWork) -> None:
    manifest = _manifest(schema_revision="0005")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    changed = _manifest(
        experiment_id=manifest.experiment_id,
        schema_revision="0005",
        configuration={"risk": {"leverage": 5}},
    )
    lifecycle = ExperimentLifecycle(changed, ExperimentStatus.CREATED, version=1)

    with pytest.raises(ImmutableManifestError):
        async with uow_factory.begin() as uow:
            await uow.experiments.transition(changed, lifecycle.start())
