"""Single-owner monotonic supervisor for immutable cloud health evaluations."""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from types import TracebackType
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

UTC = timezone.utc
WaitForStop = Callable[[asyncio.Event, float], Awaitable[bool]]


class HealthEvaluatorPort(Protocol):
    async def evaluate(self, run_id: UUID, checked_at: datetime) -> object: ...


class HealthSupervisorAlreadyRunning(RuntimeError):
    pass


class PostgresHealthOwnership(AbstractAsyncContextManager[None]):
    """Hold one session-level advisory lock for the supervisor lifetime."""

    def __init__(self, engine: AsyncEngine, *, run_id: UUID) -> None:
        if run_id.int == 0:
            raise ValueError("health supervisor run identifier cannot be nil")
        self._engine = engine
        self._run_id = run_id
        self._connection: AsyncConnection | None = None

    async def __aenter__(self) -> None:
        connection = await self._engine.connect()
        try:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 22007))"),
                {"key": f"maais:cloud-health-supervisor:v1:{self._run_id}"},
            )
        except BaseException:
            await connection.close()
            raise
        if acquired is not True:
            await connection.close()
            raise HealthSupervisorAlreadyRunning("another cloud health supervisor owns this run")
        self._connection = connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exc_type, exc_value, traceback
        connection = self._connection
        self._connection = None
        if connection is None:
            return None
        try:
            await connection.scalar(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 22007))"),
                {"key": f"maais:cloud-health-supervisor:v1:{self._run_id}"},
            )
        finally:
            await connection.close()
        return None


class HealthSupervisor:
    def __init__(
        self,
        *,
        evaluator: HealthEvaluatorPort,
        run_id: UUID,
        ownership: AbstractAsyncContextManager[None],
        interval_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        wait: WaitForStop | None = None,
    ) -> None:
        if run_id.int == 0:
            raise ValueError("health supervisor run identifier cannot be nil")
        if interval_seconds <= 0:
            raise ValueError("health supervisor interval must be positive")
        self._evaluator = evaluator
        self._run_id = run_id
        self._ownership = ownership
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._wait = wait or _wait_for_stop
        self._stop_requested = asyncio.Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def install_signal_handlers(self) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for handled_signal in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(handled_signal, self.request_stop)
            except (NotImplementedError, RuntimeError):
                continue
            installed.append(handled_signal)

        def remove() -> None:
            for handled_signal in installed:
                loop.remove_signal_handler(handled_signal)

        return remove

    async def run(self) -> None:
        next_due = self._monotonic()
        async with self._ownership:
            while not self._stop_requested.is_set():
                await self._evaluator.evaluate(self._run_id, self._utc_now())
                if self._stop_requested.is_set():
                    break
                scheduled_due = next_due + self._interval_seconds
                observed = self._monotonic()
                next_due = (
                    observed + self._interval_seconds
                    if scheduled_due <= observed
                    else scheduled_due
                )
                if await self._wait(
                    self._stop_requested,
                    max(0.0, next_due - self._monotonic()),
                ):
                    break


async def _wait_for_stop(stop_requested: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_requested.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True
