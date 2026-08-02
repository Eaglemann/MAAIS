"""Crash-safe closed-bar recovery coordination around the normal dispatch path."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from maais.execution.paper.clock import require_utc
from maais.market_data.events import ClosedBarPayload, ObservedMarketEvent
from maais.market_data.recovery import (
    BackfillBatch,
    GapRange,
    MarketCursor,
    RecoveryState,
    RecoveryStatus,
    detect_closed_bar_gap,
    validate_backfill,
)

Sleep = Callable[[float], Awaitable[None]]


class ClosedBarBackfillPort(Protocol):
    async def get_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> tuple[ObservedMarketEvent, ...]: ...


class RecoveryStateStore(Protocol):
    async def load(self, recovery_id: UUID) -> RecoveryState | None: ...

    async def save(self, recovery: RecoveryState) -> None: ...

    async def complete(
        self,
        recovery: RecoveryState,
        *,
        expected_cursor: MarketCursor,
    ) -> None: ...


class GapRecoveryError(RuntimeError):
    """Base class for a recovery that cannot safely continue."""


class GapRecoveryIdentityConflict(GapRecoveryError):
    pass


class GapRecoveryFailed(GapRecoveryError):
    def __init__(self, recovery: RecoveryState) -> None:
        super().__init__(
            f"closed-bar recovery failed after {recovery.attempt} attempts: "
            f"{recovery.failure_reason}"
        )
        self.recovery = recovery


class GapRecoveryNotCaughtUp(GapRecoveryError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveryPreparation:
    recovery: RecoveryState
    batch: BackfillBatch
    candidate: ObservedMarketEvent

    def __post_init__(self) -> None:
        if self.recovery.status is not RecoveryStatus.BACKFILLING:
            raise ValueError("prepared recovery must still be backfilling")
        if self.recovery.gap != self.batch.gap:
            raise ValueError("prepared recovery and backfill gap differ")
        payload = self.candidate.payload
        if not isinstance(payload, ClosedBarPayload) or not payload.closed:
            raise ValueError("recovery candidate must be a closed bar")
        gap = self.recovery.gap
        if (
            self.candidate.venue != gap.venue
            or self.candidate.stream != gap.stream
            or self.candidate.symbol != gap.symbol
            or payload.timeframe != gap.timeframe
            or payload.bar_open_at != gap.end_open_at_exclusive
            or self.candidate.sequence != gap.end_sequence_exclusive
        ):
            raise ValueError("recovery candidate does not immediately follow the gap")

    @property
    def dispatch_events(self) -> tuple[ObservedMarketEvent, ...]:
        return (*self.batch.events, self.candidate)


class GapRecoveryManager:
    """Persist detection before I/O and complete only after cursor catch-up."""

    def __init__(
        self,
        *,
        backfill: ClosedBarBackfillPort,
        store: RecoveryStateStore,
        now: Callable[[], datetime],
        sleep: Sleep,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 0.25,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("recovery max_attempts must be positive")
        if initial_backoff_seconds < 0:
            raise ValueError("recovery initial backoff cannot be negative")
        self._backfill = backfill
        self._store = store
        self._now = now
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds

    async def prepare(
        self,
        cursor: MarketCursor,
        candidate: ObservedMarketEvent,
    ) -> RecoveryPreparation | None:
        gap = detect_closed_bar_gap(cursor, candidate)
        if gap is None:
            return None
        recovery_id = recovery_id_for_gap(gap)
        state = await self._store.load(recovery_id)
        if state is None:
            detected_at = self._utc_now()
            state = RecoveryState.create(
                recovery_id=recovery_id,
                experiment_id=cursor.experiment_id,
                gap=gap,
                started_at=detected_at,
            )
            await self._store.save(state)
        elif state.gap != gap or state.experiment_id != cursor.experiment_id:
            raise GapRecoveryIdentityConflict(
                "persisted recovery identity differs from the detected gap"
            )

        if state.status is RecoveryStatus.COMPLETED:
            raise GapRecoveryIdentityConflict(
                "completed recovery was encountered behind the current cursor"
            )
        if state.status is RecoveryStatus.FAILED:
            raise GapRecoveryFailed(state)

        while True:
            if state.status is RecoveryStatus.DETECTED:
                state = state.begin(self._utc_now())
                await self._store.save(state)
            try:
                events = await self._backfill.get_closed_bar_events(
                    gap.symbol,
                    gap.timeframe,
                    gap.start_open_at,
                    gap.end_open_at_exclusive,
                )
                batch = validate_backfill(gap, events)
            except Exception as exc:
                reason = _failure_reason(exc)
                if state.attempt >= self._max_attempts:
                    state = state.fail(reason, self._utc_now())
                    await self._store.save(state)
                    raise GapRecoveryFailed(state) from exc
                state = state.retry(reason, self._utc_now())
                await self._store.save(state)
                delay = self._initial_backoff_seconds * (2 ** (state.attempt - 1))
                if delay:
                    await self._sleep(delay)
                continue
            return RecoveryPreparation(
                recovery=state,
                batch=batch,
                candidate=candidate,
            )

    async def complete(
        self,
        preparation: RecoveryPreparation,
        *,
        caught_up_cursor: MarketCursor,
    ) -> RecoveryState:
        _require_caught_up(preparation, caught_up_cursor)
        completed = preparation.recovery.complete(
            preparation.batch,
            self._utc_now(),
        )
        await self._store.complete(completed, expected_cursor=caught_up_cursor)
        return completed

    def _utc_now(self) -> datetime:
        value = self._now()
        require_utc(value, "recovery clock")
        return value


def recovery_id_for_gap(gap: GapRange) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "maais://market-recovery/"
        f"{gap.experiment_id}/{gap.venue}/{gap.stream}/{gap.symbol}/{gap.timeframe}/"
        f"{gap.start_sequence}/{gap.end_sequence_exclusive}/"
        f"{gap.start_open_at.isoformat()}/{gap.end_open_at_exclusive.isoformat()}",
    )


def _require_caught_up(
    preparation: RecoveryPreparation,
    cursor: MarketCursor,
) -> None:
    candidate = preparation.candidate
    payload = candidate.payload
    assert isinstance(payload, ClosedBarPayload)
    if (
        cursor.experiment_id != preparation.recovery.experiment_id
        or cursor.venue != candidate.venue
        or cursor.stream != candidate.stream
        or cursor.symbol != candidate.symbol
        or cursor.timeframe != payload.timeframe
        or cursor.event_id != candidate.event_id
        or cursor.sequence != candidate.sequence
        or cursor.bar_close_at != payload.bar_close_at
    ):
        raise GapRecoveryNotCaughtUp(
            "recovery cannot complete before the candidate cursor is durable"
        )


def _failure_reason(exc: Exception) -> str:
    message = str(exc).strip().replace("\x00", "")
    if not message:
        message = "no detail"
    return f"{type(exc).__name__}:{message}"[:1000]
