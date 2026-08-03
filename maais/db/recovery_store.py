"""PostgreSQL transaction adapter for crash-safe market recovery state."""

from __future__ import annotations

from uuid import UUID

from maais.db.unit_of_work import UnitOfWork
from maais.market_data.recovery import MarketCursor, RecoveryState
from maais.orchestration.recovery import GapRecoveryNotCaughtUp


class PostgresRecoveryStateStore:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    async def load(self, recovery_id: UUID) -> RecoveryState | None:
        async with self._unit_of_work.begin() as uow:
            try:
                return await uow.market_data.get_recovery(recovery_id)
            except LookupError:
                return None

    async def load_active(self, cursor: MarketCursor) -> RecoveryState | None:
        async with self._unit_of_work.begin() as uow:
            active = await uow.market_data.get_active_recoveries(cursor.experiment_id)
        matches = tuple(
            recovery
            for recovery in active
            if (
                recovery.gap.venue == cursor.venue
                and recovery.gap.stream == cursor.stream
                and recovery.gap.symbol == cursor.symbol
                and recovery.gap.timeframe == cursor.timeframe
            )
        )
        if len(matches) > 1:
            raise RuntimeError("multiple active recoveries exist for one cursor")
        return matches[0] if matches else None

    async def save(self, recovery: RecoveryState) -> None:
        async with self._unit_of_work.begin() as uow:
            await uow.market_data.record_recovery(recovery)

    async def complete(
        self,
        recovery: RecoveryState,
        *,
        expected_cursor: MarketCursor,
    ) -> None:
        async with self._unit_of_work.begin() as uow:
            try:
                persisted = await uow.market_data.get_cursor(
                    expected_cursor.experiment_id,
                    expected_cursor.venue,
                    expected_cursor.stream,
                    expected_cursor.symbol,
                    expected_cursor.timeframe,
                    for_update=True,
                )
            except LookupError as exc:
                raise GapRecoveryNotCaughtUp(
                    "recovery cursor does not exist at completion"
                ) from exc
            if persisted != expected_cursor:
                raise GapRecoveryNotCaughtUp(
                    "persisted recovery cursor differs from the caught-up cursor"
                )
            await uow.market_data.record_recovery(recovery)
