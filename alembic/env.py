import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

import maais.compliance.models  # noqa: F401
import maais.db.models  # noqa: F401

# Import all ORM model modules so Base.metadata is populated before migrations run.
import maais.market_data.models  # noqa: F401
from alembic import context
from maais.config.settings import get_settings
from maais.db.connection import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, _parent_names: dict[str, str | None]) -> bool:
    if type_ == "schema":
        return name in (None, "public", "maais_auth")
    return True


def get_url() -> str:
    override = config.attributes.get("database_url")
    if isinstance(override, str) and override:
        return override
    return get_settings().database_url_value


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
