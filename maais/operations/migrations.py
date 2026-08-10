"""Guarded cloud role bootstrap and Alembic migration orchestration."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterator

from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from alembic import command
from maais.core.logging import get_logger
from maais.db.roles import (
    DatabaseRolePasswords,
    bootstrap_database_principals,
    bootstrap_database_roles,
)

MIGRATION_LOCK_KEY = 5_321_109_104_001_922_019
MAINTENANCE_LOCK_TIMEOUT_MS = 10_000
MAINTENANCE_STATEMENT_TIMEOUT_MS = 120_000

logger = get_logger(__name__)


class SchemaIdentityError(RuntimeError):
    pass


class ActiveRunBlocksMaintenance(RuntimeError):
    pass


class DatabaseAuthorityError(RuntimeError):
    pass


type ScalarExecutor = AsyncConnection | AsyncSession


async def assert_expected_schema(executor: ScalarExecutor, expected: str) -> None:
    if re.fullmatch(r"\d{4}", expected) is None:
        raise ValueError("expected schema revision must be four decimal digits")
    actual = str(await executor.scalar(text("SELECT version_num FROM alembic_version")))
    if actual != expected:
        raise SchemaIdentityError(f"database schema mismatch: expected={expected} actual={actual}")


async def ensure_no_active_runs(executor: ScalarExecutor) -> None:
    relation = await executor.scalar(text("SELECT to_regclass('public.run_instances')"))
    if relation is None:
        return
    active = await executor.scalar(
        text("SELECT count(*) FROM public.run_instances WHERE status = 'active'")
    )
    if type(active) is not int:
        raise RuntimeError("active run count query returned an invalid value")
    if active:
        raise ActiveRunBlocksMaintenance(
            f"database maintenance is blocked by {active} active run instance(s)"
        )


@asynccontextmanager
async def migration_advisory_lock(connection: AsyncConnection) -> AsyncIterator[None]:
    await connection.execute(
        text("SELECT pg_advisory_lock(:lock_key)"),
        {"lock_key": MIGRATION_LOCK_KEY},
    )
    await connection.commit()
    try:
        yield
    finally:
        released = await connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": MIGRATION_LOCK_KEY},
        )
        await connection.commit()
        if released is not True:
            raise RuntimeError("cloud migration advisory lock was not held")


async def bootstrap_roles_with_url(
    database_url: str,
    passwords: DatabaseRolePasswords,
) -> tuple[str, ...]:
    engine = create_async_engine(
        _database_url_with_maintenance_timeouts(database_url),
        pool_pre_ping=True,
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            async with migration_advisory_lock(connection):
                await ensure_no_active_runs(connection)
                authorized = await connection.scalar(
                    text(
                        "SELECT rolsuper OR rolcreaterole FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
                if authorized is not True:
                    raise DatabaseAuthorityError(
                        "role bootstrap requires a PostgreSQL role administrator"
                    )
                await connection.commit()
                async with connection.begin():
                    await bootstrap_database_roles(connection, passwords)
        return (
            "maais_migrator",
            "maais_worker",
            "maais_web",
            "maais_ops",
            "maais_verifier",
        )
    finally:
        await engine.dispose()


async def initialize_database_with_url(
    administrator_database_url: str,
    passwords: DatabaseRolePasswords,
    *,
    expected_revision: str,
    repository_root: Path,
) -> tuple[str, tuple[str, ...]]:
    """Create principals, migrate as the migrator, then finalize runtime grants."""

    logger.info(
        "cloud_database_bootstrap_stage",
        stage="principals",
        operation_id="bootstrap:principals",
        outcome="started",
    )
    await _bootstrap_principals_with_url(administrator_database_url, passwords)
    logger.info(
        "cloud_database_bootstrap_stage",
        stage="principals",
        operation_id="bootstrap:principals",
        outcome="completed",
    )
    migrator_database_url = _database_url_for_role(
        administrator_database_url,
        role_name="maais_migrator",
        password=passwords.migrator,
    )
    logger.info(
        "cloud_database_bootstrap_stage",
        stage="migration",
        operation_id="bootstrap:migration",
        outcome="started",
    )
    revision = await migrate_with_url(
        migrator_database_url,
        expected_revision=expected_revision,
        repository_root=repository_root,
    )
    logger.info(
        "cloud_database_bootstrap_stage",
        stage="migration",
        operation_id="bootstrap:migration",
        outcome="completed",
        reason_code=f"schema_revision_{revision}",
    )
    logger.info(
        "cloud_database_bootstrap_stage",
        stage="final_grants",
        operation_id="bootstrap:final_grants",
        outcome="started",
    )
    roles = await bootstrap_roles_with_url(administrator_database_url, passwords)
    logger.info(
        "cloud_database_bootstrap_stage",
        stage="final_grants",
        operation_id="bootstrap:final_grants",
        outcome="completed",
    )
    return revision, roles


async def _bootstrap_principals_with_url(
    database_url: str,
    passwords: DatabaseRolePasswords,
) -> None:
    engine = create_async_engine(
        _database_url_with_maintenance_timeouts(database_url),
        pool_pre_ping=True,
        hide_parameters=True,
    )
    try:
        async with engine.connect() as connection:
            async with migration_advisory_lock(connection):
                await ensure_no_active_runs(connection)
                authorized = await connection.scalar(
                    text(
                        "SELECT rolsuper OR rolcreaterole FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
                if authorized is not True:
                    raise DatabaseAuthorityError(
                        "role bootstrap requires a PostgreSQL role administrator"
                    )
                await connection.commit()
                async with connection.begin():
                    await bootstrap_database_principals(connection, passwords)
    finally:
        await engine.dispose()


def _database_url_for_role(
    administrator_database_url: str,
    *,
    role_name: str,
    password: str,
) -> str:
    url = make_url(administrator_database_url)
    if url.get_backend_name() != "postgresql" or not url.database or not url.host:
        raise ValueError("database initialization requires one PostgreSQL network URL")
    return url.set(
        drivername="postgresql+psycopg",
        username=role_name,
        password=password,
    ).render_as_string(hide_password=False)


def _database_url_with_maintenance_timeouts(database_url: str) -> str:
    """Bound every bootstrap or migration statement without exposing credentials."""

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or not url.database or not url.host:
        raise ValueError("cloud database maintenance requires one PostgreSQL network URL")
    existing_options = str(url.query.get("options", "")).strip()
    bounded_options = " ".join(
        part
        for part in (
            existing_options,
            f"-c lock_timeout={MAINTENANCE_LOCK_TIMEOUT_MS}ms",
            f"-c statement_timeout={MAINTENANCE_STATEMENT_TIMEOUT_MS}ms",
        )
        if part
    )
    return url.update_query_dict({"options": bounded_options}).render_as_string(hide_password=False)


async def migrate_with_url(
    database_url: str,
    *,
    expected_revision: str,
    repository_root: Path,
) -> str:
    if re.fullmatch(r"\d{4}", expected_revision) is None:
        raise ValueError("expected schema revision must be four decimal digits")
    config_path = repository_root / "alembic.ini"
    if not config_path.is_file():
        raise ValueError("repository root does not contain alembic.ini")
    bounded_database_url = _database_url_with_maintenance_timeouts(database_url)
    return await asyncio.to_thread(
        _migrate_with_url_synchronously,
        bounded_database_url,
        expected_revision=expected_revision,
        config_path=config_path,
    )


def _migrate_with_url_synchronously(
    database_url: str,
    *,
    expected_revision: str,
    config_path: Path,
) -> str:
    """Keep Alembic and its advisory-lock connection on one blocking thread."""

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            current_user = str(connection.scalar(text("SELECT current_user")))
            if current_user != "maais_migrator":
                raise DatabaseAuthorityError("cloud migration must connect as maais_migrator")
            connection.commit()
            with _migration_advisory_lock_synchronously(connection):
                _ensure_no_active_runs_synchronously(connection)
                connection.commit()
                _upgrade_to_head(connection, config_path)
                _ensure_no_active_runs_synchronously(connection)
                _assert_expected_schema_synchronously(connection, expected_revision)
                connection.commit()
        return expected_revision
    finally:
        engine.dispose()


@contextmanager
def _migration_advisory_lock_synchronously(connection: Connection) -> Iterator[None]:
    connection.execute(
        text("SELECT pg_advisory_lock(:lock_key)"),
        {"lock_key": MIGRATION_LOCK_KEY},
    )
    connection.commit()
    try:
        yield
    finally:
        released = connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": MIGRATION_LOCK_KEY},
        )
        connection.commit()
        if released is not True:
            raise RuntimeError("cloud migration advisory lock was not held")


def _ensure_no_active_runs_synchronously(connection: Connection) -> None:
    relation = connection.scalar(text("SELECT to_regclass('public.run_instances')"))
    if relation is None:
        return
    active = connection.scalar(
        text("SELECT count(*) FROM public.run_instances WHERE status = 'active'")
    )
    if type(active) is not int:
        raise RuntimeError("active run count query returned an invalid value")
    if active:
        raise ActiveRunBlocksMaintenance(
            f"database maintenance is blocked by {active} active run instance(s)"
        )


def _assert_expected_schema_synchronously(connection: Connection, expected: str) -> None:
    actual = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
    if actual != expected:
        raise SchemaIdentityError(f"database schema mismatch: expected={expected} actual={actual}")


def _upgrade_to_head(connection: Connection, config_path: Path) -> None:
    config = Config(str(config_path))
    config.set_main_option("script_location", str(config_path.parent / "alembic"))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
