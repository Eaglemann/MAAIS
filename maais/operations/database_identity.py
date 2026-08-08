"""PostgreSQL cluster identity used to bind runtime and container health."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from maais.config.settings import get_settings


async def collect_database_identity(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT current_database() AS database, "
                            "system_identifier::text AS system_identifier, "
                            "inet_server_addr()::text AS server_address, "
                            "inet_server_port() AS server_port "
                            "FROM pg_control_system()"
                        )
                    )
                )
                .mappings()
                .one()
            )
    return {
        "database": str(row["database"]),
        "system_identifier": str(row["system_identifier"]),
        "server_address": str(row["server_address"]),
        "server_port": int(row["server_port"]),
    }


async def collect_configured_database_identity() -> dict[str, object]:
    engine = create_async_engine(get_settings().database_url_value, pool_pre_ping=True)
    try:
        return await collect_database_identity(engine)
    finally:
        await engine.dispose()
