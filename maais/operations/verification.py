"""Read-only operator verification for the authoritative PostgreSQL ledger."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from maais.config.settings import get_settings
from maais.db.replay import LedgerConsistencyReport, verify_ledger_consistency

_READ_ONLY_SNAPSHOT = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


async def establish_read_only_snapshot(session: AsyncSession) -> None:
    """Pin one stable PostgreSQL snapshot for a multi-statement operational read."""
    await session.execute(_READ_ONLY_SNAPSHOT)


def ledger_consistency_payload(report: LedgerConsistencyReport) -> dict[str, object]:
    """Serialize a consistency report without losing aggregate identity."""
    errors = [
        {
            "code": error.code,
            "aggregate_type": error.aggregate_type,
            "aggregate_id": str(error.aggregate_id) if error.aggregate_id is not None else None,
            "details": error.details,
        }
        for error in report.errors
    ]
    return {"ok": report.ok, "error_count": len(errors), "errors": errors}


async def verify_ledger_with_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, object]:
    """Verify all event/projection invariants inside a read-only transaction."""
    async with session_factory() as session:
        async with session.begin():
            await establish_read_only_snapshot(session)
            report = await verify_ledger_consistency(session)
    return ledger_consistency_payload(report)


async def verify_configured_ledger() -> dict[str, object]:
    """Verify the configured database using an isolated, quiet connection pool."""
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return await verify_ledger_with_factory(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
