from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from maais.db.unit_of_work import UnitOfWork

_PHASE_ONE_TABLES = (
    "worker_leases",
    "worker_checkpoints",
    "incidents",
    "market_recovery_runs",
    "data_quality_evaluations",
    "market_cursors",
    "counterfactuals",
    "execution_sensitivities",
    "funding_entries",
    "account_snapshots",
    "exit_plans",
    "position_lots",
    "positions",
    "fills",
    "order_events",
    "order_intents",
    "trade_proposals",
    "gate_evaluations",
    "decision_summaries",
    "agent_evaluations",
    "decision_cycles",
    "market_frames",
    "outbox_events",
    "domain_events",
    "event_streams",
    "agent_versions",
    "strategy_versions",
    "experiments",
)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    value = os.environ.get("MAAIS_TEST_DATABASE_URL")
    if not value:
        pytest.skip("MAAIS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    database = make_url(value).database or ""
    if not database.endswith("_test"):
        pytest.fail("integration database name must end with _test")
    return value


@pytest_asyncio.fixture(scope="session")
async def db_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database_url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_phase_one_tables(db_engine: AsyncEngine) -> AsyncIterator[None]:
    table_list = ", ".join(_PHASE_ONE_TABLES)
    async with db_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
    yield
    async with db_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_connection(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with db_engine.connect() as connection:
        yield connection


@pytest.fixture
def uow_factory(db_engine: AsyncEngine) -> UnitOfWork:
    return UnitOfWork(async_sessionmaker(db_engine, expire_on_commit=False))
