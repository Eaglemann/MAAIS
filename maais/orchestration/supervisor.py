"""Fail-closed lifecycle owner for the official live paper worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from maais.db.unit_of_work import UnitOfWork, UnitOfWorkContext
from maais.execution.paper.clock import require_utc
from maais.execution.paper.exits import ExitPlanStatus
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import ObservedMarketEvent
from maais.market_data.recovery import MarketCursor
from maais.orchestration.checkpoints import WorkerCheckpoint, WorkerLease, WorkerStatus

Sleep = Callable[[float], Awaitable[None]]


class PublicDataPort(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def events(self) -> AsyncIterator[ObservedMarketEvent]: ...


class ObservationPort(Protocol):
    async def observe(self, event: ObservedMarketEvent) -> bool: ...


class DispatchEnginePort(Protocol):
    @property
    def cursors(self) -> Mapping[object, MarketCursor]: ...

    async def process(self, event: ObservedMarketEvent) -> object: ...


class PaperWorkerSupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"


class PaperWorkerHalt(RuntimeError):
    pass


class _DispatchStop:
    pass


class PaperWorkerSupervisor:
    """Own the worker lease, serialized event pump, and durable lifecycle."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        manifest: ExperimentManifest,
        worker_id: UUID,
        public_data: PublicDataPort,
        observations: ObservationPort,
        engine: DispatchEnginePort,
        now: Callable[[], datetime] | None = None,
        sleep: Sleep = asyncio.sleep,
        lease_ttl: timedelta = timedelta(seconds=30),
        lease_renew_interval: timedelta = timedelta(seconds=10),
        drain_timeout: timedelta = timedelta(seconds=30),
        dispatch_queue_size: int = 10_000,
    ) -> None:
        policy = LivePaperPolicy.from_manifest(manifest)
        if worker_id.int == 0:
            raise ValueError("paper worker_id cannot be nil")
        if lease_renew_interval <= timedelta(0):
            raise ValueError("paper worker lease renewal interval must be positive")
        if lease_ttl <= lease_renew_interval:
            raise ValueError("paper worker lease TTL must exceed its renewal interval")
        if drain_timeout <= timedelta(0):
            raise ValueError("paper worker drain timeout must be positive")
        if dispatch_queue_size <= 0:
            raise ValueError("paper worker dispatch queue size must be positive")
        self._uow = uow
        self._manifest = manifest
        self._policy = policy
        self._worker_id = worker_id
        self._public_data = public_data
        self._observations = observations
        self._engine = engine
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._lease_ttl = lease_ttl
        self._renew_interval = lease_renew_interval
        self._drain_timeout = drain_timeout
        self._state = PaperWorkerSupervisorState.STOPPED
        self._checkpoint: WorkerCheckpoint | None = None
        self._lease: WorkerLease | None = None
        self._dispatch_queue: asyncio.Queue[ObservedMarketEvent | _DispatchStop] = (
            asyncio.Queue(maxsize=dispatch_queue_size)
        )
        self._dispatch_stop = _DispatchStop()
        self._ingest_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._failure: PaperWorkerHalt | None = None

    @property
    def state(self) -> PaperWorkerSupervisorState:
        return self._state

    @property
    def failure(self) -> PaperWorkerHalt | None:
        return self._failure

    async def start(self) -> None:
        if self._state is not PaperWorkerSupervisorState.STOPPED or any(
            task is not None
            for task in (
                self._ingest_task,
                self._dispatch_task,
                self._heartbeat_task,
                self._monitor_task,
            )
        ):
            raise RuntimeError("paper worker can be started exactly once")
        self._state = PaperWorkerSupervisorState.STARTING
        try:
            await self._start_persistence()
            await self._public_data.start()
            await self._transition(WorkerStatus.RUNNING)
        except Exception as exc:
            await self._halt(exc)
            assert self._failure is not None
            raise self._failure from exc

        self._state = PaperWorkerSupervisorState.RUNNING
        self._ingest_task = asyncio.create_task(
            self._ingest(),
            name="paper_worker_event_ingest",
        )
        self._dispatch_task = asyncio.create_task(
            self._dispatch(),
            name="paper_worker_event_dispatch",
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(),
            name="paper_worker_lease_heartbeat",
        )
        self._monitor_task = asyncio.create_task(
            self._monitor(),
            name="paper_worker_supervisor",
        )

    async def stop(self) -> None:
        if self._state is PaperWorkerSupervisorState.STOPPED:
            return
        if self._state is PaperWorkerSupervisorState.HALTED:
            assert self._failure is not None
            raise self._failure
        if self._state is not PaperWorkerSupervisorState.RUNNING:
            raise RuntimeError("paper worker is not running")

        self._stopping = True
        self._state = PaperWorkerSupervisorState.STOPPING
        await self._transition(WorkerStatus.STOPPING)
        try:
            await self._public_data.stop()
            async with asyncio.timeout(self._drain_timeout.total_seconds()):
                if self._ingest_task is not None:
                    await self._ingest_task
                if self._dispatch_task is not None:
                    await self._dispatch_task
        except Exception as exc:
            await self._halt(exc)
            assert self._failure is not None
            raise self._failure from exc

        await self._cancel_heartbeat()
        if self._monitor_task is not None:
            await self._monitor_task
        await self._stop_persistence()
        self._state = PaperWorkerSupervisorState.STOPPED

    async def wait_closed(self) -> None:
        if self._monitor_task is not None:
            await self._monitor_task
        if self._failure is not None:
            raise self._failure

    async def _start_persistence(self) -> None:
        started_at = self._observed_now()
        async with self._uow.begin() as transaction:
            self._lease = await transaction.workers.acquire(
                experiment_id=self._manifest.experiment_id,
                worker_id=self._worker_id,
                acquired_at=started_at,
                ttl=self._lease_ttl,
            )
            await transaction.controls.initialize(
                self._manifest.experiment_id,
                initialized_at=started_at,
                actor=f"paper_worker:{self._worker_id}",
            )
            state = self._checkpoint_state()
            try:
                previous = await transaction.orchestration.get_checkpoint(
                    self._manifest.experiment_id
                )
            except LookupError:
                self._checkpoint = WorkerCheckpoint.create(
                    experiment_id=self._manifest.experiment_id,
                    worker_id=self._worker_id,
                    checkpoint_at=started_at,
                    state=state,
                )
            else:
                self._checkpoint = previous.restart(
                    worker_id=self._worker_id,
                    checkpoint_at=started_at,
                    state=state,
                )
            await transaction.orchestration.record_checkpoint(self._checkpoint)
        async with self._uow.begin() as transaction:
            await self._audit_recovered_state(transaction)
            await transaction.experiments.ensure_running(
                self._manifest,
                started_at=self._observed_now(),
            )

    async def _audit_recovered_state(self, transaction: UnitOfWorkContext) -> None:
        account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
        if account.leverage != self._policy.leverage:
            raise PaperWorkerHalt("restored account leverage differs from run policy")
        pending = await transaction.paper_execution.load_pending_orders(
            self._manifest.experiment_id
        )
        if pending:
            raise PaperWorkerHalt("pending paper orders require explicit startup reconciliation")
        plans = await transaction.paper_execution.load_open_exit_plans(
            self._manifest.experiment_id
        )
        plans_by_position = {plan.position_id: plan for plan in plans}
        if len(plans_by_position) != len(plans):
            raise PaperWorkerHalt("protective exit plans are duplicated by position")
        open_positions = {
            position.position_id: position
            for position in account.positions.values()
            if not position.is_flat
        }
        if set(plans_by_position) != set(open_positions):
            raise PaperWorkerHalt(
                "every open paper position requires exactly one protective exit plan"
            )
        if any(plan.status is ExitPlanStatus.TRIGGERED for plan in plans):
            raise PaperWorkerHalt(
                "triggered protective exits require explicit startup reconciliation"
            )

    async def _ingest(self) -> None:
        try:
            async for event in self._public_data.events():
                await self._observations.observe(event)
                try:
                    self._dispatch_queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise PaperWorkerHalt(
                        "paper worker dispatch queue reached capacity"
                    ) from exc
        finally:
            await self._dispatch_queue.put(self._dispatch_stop)

    async def _dispatch(self) -> None:
        while True:
            event = await self._dispatch_queue.get()
            if event is self._dispatch_stop:
                return
            if not isinstance(event, ObservedMarketEvent):
                raise TypeError("paper worker dispatch queue contains an invalid item")
            await self._engine.process(event)

    async def _heartbeat(self) -> None:
        while True:
            await self._sleep(self._renew_interval.total_seconds())
            heartbeat_at = self._observed_now()
            async with self._uow.begin() as transaction:
                self._lease = await transaction.workers.renew(
                    experiment_id=self._manifest.experiment_id,
                    worker_id=self._worker_id,
                    heartbeat_at=heartbeat_at,
                    ttl=self._lease_ttl,
                )

    async def _monitor(self) -> None:
        assert (
            self._ingest_task is not None
            and self._dispatch_task is not None
            and self._heartbeat_task is not None
        )
        done, _ = await asyncio.wait(
            (self._ingest_task, self._dispatch_task, self._heartbeat_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._stopping:
            return
        task = next(
            (
                candidate
                for candidate in done
                if not candidate.cancelled() and candidate.exception() is not None
            ),
            next(iter(done)),
        )
        failure = (
            asyncio.CancelledError()
            if task.cancelled()
            else task.exception()
        )
        if failure is None:
            failure = RuntimeError(f"paper worker task ended unexpectedly: {task.get_name()}")
        await self._halt(failure)

    async def _transition(self, status: WorkerStatus) -> None:
        if self._checkpoint is None:
            raise RuntimeError("paper worker checkpoint is not initialized")
        checkpoint = self._checkpoint.transition(
            status,
            self._observed_now(),
            self._checkpoint_state(),
        )
        async with self._uow.begin() as transaction:
            await transaction.orchestration.record_checkpoint(checkpoint)
        self._checkpoint = checkpoint

    async def _stop_persistence(self) -> None:
        if self._checkpoint is None:
            raise RuntimeError("paper worker checkpoint is not initialized")
        stopped_at = self._observed_now()
        checkpoint = self._checkpoint.transition(
            WorkerStatus.STOPPED,
            stopped_at,
            self._checkpoint_state(),
        )
        async with self._uow.begin() as transaction:
            await transaction.orchestration.record_checkpoint(checkpoint)
            self._lease = await transaction.workers.release(
                experiment_id=self._manifest.experiment_id,
                worker_id=self._worker_id,
                released_at=stopped_at,
            )
        self._checkpoint = checkpoint

    async def _halt(self, exc: BaseException) -> None:
        if self._state is PaperWorkerSupervisorState.HALTED:
            return
        self._stopping = True
        self._state = PaperWorkerSupervisorState.HALTED
        detail = _failure_detail(exc)
        self._failure = PaperWorkerHalt(f"paper worker halted: {detail}")
        try:
            await self._public_data.stop()
        except Exception:
            pass
        await self._cancel_background_tasks()
        if self._checkpoint is None:
            return
        halted_at = self._observed_now()
        checkpoint = self._checkpoint.transition(
            WorkerStatus.HALTED,
            halted_at,
            {**self._checkpoint_state(), "failure": detail},
        )
        async with self._uow.begin() as transaction:
            await transaction.controls.halt(
                self._manifest.experiment_id,
                reason=f"paper_worker:{detail}",
                halted_at=halted_at,
                actor="paper_worker",
            )
            await transaction.orchestration.record_checkpoint(checkpoint)
            self._lease = await transaction.workers.release(
                experiment_id=self._manifest.experiment_id,
                worker_id=self._worker_id,
                released_at=halted_at,
            )
        self._checkpoint = checkpoint

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (
                self._ingest_task,
                self._dispatch_task,
                self._heartbeat_task,
            )
            if task is not None and task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            return
        self._heartbeat_task.cancel()
        await asyncio.gather(self._heartbeat_task, return_exceptions=True)

    def _checkpoint_state(self) -> dict[str, object]:
        cursors = tuple(
            sorted(
                (
                    cursor.venue,
                    cursor.stream,
                    cursor.symbol,
                    cursor.timeframe,
                    cursor.event_id,
                    cursor.sequence,
                )
                for cursor in self._engine.cursors.values()
            )
        )
        return {
            "lease_epoch": self._lease.epoch if self._lease is not None else None,
            "cursor_count": len(cursors),
            "cursors": cursors,
            "dispatch_queue_depth": self._dispatch_queue.qsize(),
        }

    def _observed_now(self) -> datetime:
        value = self._now()
        require_utc(value, "paper worker observed time")
        return value


def _failure_detail(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}:{message}" if message else type(exc).__name__
