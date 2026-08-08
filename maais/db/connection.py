from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from maais.config.settings import get_settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


def _make_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url_value,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=not settings.is_production,
    )


def _make_session_factory(engine=None):
    if engine is None:
        engine = _make_engine()
    return async_sessionmaker(engine, expire_on_commit=False)


_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = _make_session_factory(get_engine())
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
