"""Fail-closed lifecycle owner for the official live paper worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from maais.db.unit_of_work import UnitOfWork, UnitOfWorkContext
from maais.domain.enums import ExperimentStatus
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
    def cursors(self) -> Mapping[tuple[str, str, str, str], MarketCursor]: ...

    async def process(self, event: ObservedMarketEvent) -> object: ...


class OperatorCommandResultPort(Protocol):
    @property
    def stop_worker(self) -> bool: ...

    @property
    def activate_worker(self) -> bool: ...


class OperatorCommandPort(Protocol):
    async def execute_next(self) -> OperatorCommandResultPort | None: ...


class PaperWorkerSupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    STANDBY = "standby"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"


class PaperWorkerHalt(RuntimeError):
    """Terminal worker failure with explicit halt-persistence evidence."""

    def __init__(
        self,
        message: str,
        *,
        original_exception: BaseException | None = None,
        persistence_error: BaseException | None = None,
        halt_persistence_outcome: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.original_exception = original_exception
        self.persistence_error = persistence_error
        self.halt_persistence_outcome = halt_persistence_outcome

    @classmethod
    def from_terminal_failure(
        cls,
        original_exception: BaseException,
        *,
        persistence_error: BaseException | None = None,
        persistence_succeeded: bool | None = None,
    ) -> PaperWorkerHalt:
        detail = _failure_detail(original_exception)
        message = f"paper worker halted: {detail}"
        if persistence_error is not None:
            message = f"{message}; halt persistence failed: {_failure_detail(persistence_error)}"
            outcome = "halt_persistence_failed"
        elif persistence_succeeded is True:
            outcome = "halt_persistence_succeeded"
        elif persistence_succeeded is False:
            outcome = "halt_persistence_unavailable"
        else:
            outcome = "halt_persistence_not_attempted"
        failure = cls(
            message,
            original_exception=original_exception,
            persistence_error=persistence_error,
            halt_persistence_outcome=outcome,
        )
        failure.__cause__ = (
            BaseExceptionGroup(
                "paper worker terminal and halt-persistence failures",
                (original_exception, persistence_error),
            )
            if persistence_error is not None
            else original_exception
        )
        return failure


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
        operator_commands: OperatorCommandPort | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Sleep = asyncio.sleep,
        lease_ttl: timedelta = timedelta(seconds=30),
        lease_renew_interval: timedelta = timedelta(seconds=10),
        checkpoint_interval: timedelta = timedelta(seconds=60),
        drain_timeout: timedelta = timedelta(seconds=30),
        dispatch_queue_size: int = 10_000,
        command_poll_interval: timedelta = timedelta(milliseconds=250),
    ) -> None:
        policy = LivePaperPolicy.from_manifest(manifest)
        if worker_id.int == 0:
            raise ValueError("paper worker_id cannot be nil")
        if lease_renew_interval <= timedelta(0):
            raise ValueError("paper worker lease renewal interval must be positive")
        if lease_ttl <= lease_renew_interval:
            raise ValueError("paper worker lease TTL must exceed its renewal interval")
        if checkpoint_interval <= timedelta(0):
            raise ValueError("paper worker checkpoint interval must be positive")
        if drain_timeout <= timedelta(0):
            raise ValueError("paper worker drain timeout must be positive")
        if dispatch_queue_size <= 0:
            raise ValueError("paper worker dispatch queue size must be positive")
        if command_poll_interval <= timedelta(0):
            raise ValueError("operator command poll interval must be positive")
        self._uow = uow
        self._manifest = manifest
        self._policy = policy
        self._worker_id = worker_id
        self._public_data = public_data
        self._observations = observations
        self._engine = engine
        self._operator_commands = operator_commands
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._lease_ttl = lease_ttl
        self._renew_interval = lease_renew_interval
        self._checkpoint_interval = checkpoint_interval
        self._drain_timeout = drain_timeout
        self._command_poll_interval = command_poll_interval
        self._state = PaperWorkerSupervisorState.STOPPED
        self._checkpoint: WorkerCheckpoint | None = None
        self._lease: WorkerLease | None = None
        self._dispatch_queue: asyncio.Queue[ObservedMarketEvent | _DispatchStop] = asyncio.Queue(
            maxsize=dispatch_queue_size
        )
        self._dispatch_stop = _DispatchStop()
        self._ingest_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._command_task: asyncio.Task[None] | None = None
        self._data_active = asyncio.Event()
        self._operator_stop_requested = asyncio.Event()
        self._serialization_lock = asyncio.Lock()
        self._stopping = False
        self._failure: PaperWorkerHalt | None = None

    @property
    def state(self) -> PaperWorkerSupervisorState:
        return self._state

    @property
    def failure(self) -> PaperWorkerHalt | None:
        return self._failure

    @property
    def operator_stop_requested(self) -> asyncio.Event:
        return self._operator_stop_requested

    async def start(self) -> None:
        if self._state is not PaperWorkerSupervisorState.STOPPED or any(
            task is not None
            for task in (
                self._ingest_task,
                self._dispatch_task,
                self._heartbeat_task,
                self._checkpoint_task,
                self._monitor_task,
                self._command_task,
            )
        ):
            raise RuntimeError("paper worker can be started exactly once")
        self._state = PaperWorkerSupervisorState.STARTING
        try:
            activate_data = await self._start_persistence()
            await self._transition(WorkerStatus.RUNNING)
            if activate_data:
                await self._activate_data()
        except Exception as exc:
            await self._halt(exc)
            assert self._failure is not None
            raise self._failure

        self._state = (
            PaperWorkerSupervisorState.RUNNING
            if self._data_active.is_set()
            else PaperWorkerSupervisorState.STANDBY
        )
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
        self._checkpoint_task = asyncio.create_task(
            self._checkpoint_snapshots(),
            name="paper_worker_checkpoint_snapshots",
        )
        if self._operator_commands is not None:
            self._command_task = asyncio.create_task(
                self._poll_operator_commands(),
                name="paper_worker_operator_commands",
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
        if self._state not in {
            PaperWorkerSupervisorState.RUNNING,
            PaperWorkerSupervisorState.STANDBY,
        }:
            raise RuntimeError("paper worker is not running")

        self._stopping = True
        self._state = PaperWorkerSupervisorState.STOPPING
        await self._cancel_operator_commands()
        await self._cancel_checkpoint_snapshots()
        await self._transition(WorkerStatus.STOPPING)
        try:
            if self._data_active.is_set():
                await self._public_data.stop()
                async with asyncio.timeout(self._drain_timeout.total_seconds()):
                    if self._ingest_task is not None:
                        await self._ingest_task
                    if self._dispatch_task is not None:
                        await self._dispatch_task
            else:
                for task in (self._ingest_task, self._dispatch_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (self._ingest_task, self._dispatch_task) if task),
                    return_exceptions=True,
                )
        except Exception as exc:
            await self._halt(exc)
            assert self._failure is not None
            raise self._failure

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

    async def _start_persistence(self) -> bool:
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
            status = await transaction.experiments.get_status(self._manifest.experiment_id)
            if self._operator_commands is None:
                await transaction.experiments.ensure_running(
                    self._manifest,
                    started_at=self._observed_now(),
                )
                return True
            if status is ExperimentStatus.CREATED:
                return False
            if status in {ExperimentStatus.RUNNING, ExperimentStatus.PAUSED}:
                return True
            raise PaperWorkerHalt(f"paper worker cannot recover experiment from {status.value}")

    async def _audit_recovered_state(self, transaction: UnitOfWorkContext) -> None:
        account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
        if account.leverage != self._policy.leverage:
            raise PaperWorkerHalt("restored account leverage differs from run policy")
        pending = await transaction.paper_execution.load_pending_orders(
            self._manifest.experiment_id
        )
        if pending:
            raise PaperWorkerHalt("pending paper orders require explicit startup reconciliation")
        plans = await transaction.paper_execution.load_open_exit_plans(self._manifest.experiment_id)
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
        await self._data_active.wait()
        try:
            async for event in self._public_data.events():
                await self._observations.observe(event)
                try:
                    self._dispatch_queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise PaperWorkerHalt("paper worker dispatch queue reached capacity") from exc
        finally:
            await self._dispatch_queue.put(self._dispatch_stop)

    async def _dispatch(self) -> None:
        await self._data_active.wait()
        while True:
            event = await self._dispatch_queue.get()
            if event is self._dispatch_stop:
                return
            if not isinstance(event, ObservedMarketEvent):
                raise TypeError("paper worker dispatch queue contains an invalid item")
            async with self._serialization_lock:
                await self._engine.process(event)

    async def _poll_operator_commands(self) -> None:
        if self._operator_commands is None:
            return
        while True:
            async with self._serialization_lock:
                execution = await self._operator_commands.execute_next()
                if execution is not None:
                    if execution.activate_worker:
                        await self._activate_data()
                    if execution.stop_worker:
                        self._operator_stop_requested.set()
            await self._sleep(self._command_poll_interval.total_seconds())

    async def _activate_data(self) -> None:
        if self._data_active.is_set():
            return
        await self._public_data.start()
        self._data_active.set()
        if self._state is PaperWorkerSupervisorState.STANDBY:
            self._state = PaperWorkerSupervisorState.RUNNING

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

    async def _checkpoint_snapshots(self) -> None:
        while True:
            await self._sleep(self._checkpoint_interval.total_seconds())
            if self._checkpoint is None:
                raise RuntimeError("paper worker checkpoint is not initialized")
            checkpoint = self._checkpoint.snapshot(
                self._observed_now(),
                self._checkpoint_state(),
            )
            async with self._uow.begin() as transaction:
                await transaction.orchestration.record_checkpoint(checkpoint)
                self._checkpoint = checkpoint

    async def _monitor(self) -> None:
        assert (
            self._ingest_task is not None
            and self._dispatch_task is not None
            and self._heartbeat_task is not None
            and self._checkpoint_task is not None
        )
        tasks = (
            self._ingest_task,
            self._dispatch_task,
            self._heartbeat_task,
            self._checkpoint_task,
            *((self._command_task,) if self._command_task is not None else ()),
        )
        done, _ = await asyncio.wait(
            tasks,
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
        failure = asyncio.CancelledError() if task.cancelled() else task.exception()
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
        self._failure = PaperWorkerHalt.from_terminal_failure(exc)
        try:
            await self._public_data.stop()
        except Exception:
            pass
        await self._cancel_background_tasks()
        if self._checkpoint is None:
            self._failure = PaperWorkerHalt.from_terminal_failure(
                exc,
                persistence_succeeded=False,
            )
            return
        halted_at = self._observed_now()
        checkpoint = self._checkpoint.transition(
            WorkerStatus.HALTED,
            halted_at,
            {**self._checkpoint_state(), "failure": detail},
        )
        try:
            async with self._uow.begin() as transaction:
                status = await transaction.experiments.get_status(self._manifest.experiment_id)
                if status in {ExperimentStatus.RUNNING, ExperimentStatus.PAUSED}:
                    await transaction.experiments.fail_active(
                        self._manifest,
                        reason=f"paper_worker:{detail}",
                        failed_at=halted_at,
                    )
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
        except Exception as persistence_error:
            self._failure = PaperWorkerHalt.from_terminal_failure(
                exc,
                persistence_error=persistence_error,
            )
            return
        self._checkpoint = checkpoint
        self._failure = PaperWorkerHalt.from_terminal_failure(
            exc,
            persistence_succeeded=True,
        )

    async def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in (
                self._ingest_task,
                self._dispatch_task,
                self._heartbeat_task,
                self._checkpoint_task,
                self._command_task,
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

    async def _cancel_checkpoint_snapshots(self) -> None:
        if self._checkpoint_task is None or self._checkpoint_task.done():
            return
        self._checkpoint_task.cancel()
        await asyncio.gather(self._checkpoint_task, return_exceptions=True)

    async def _cancel_operator_commands(self) -> None:
        if self._command_task is None or self._command_task.done():
            return
        self._command_task.cancel()
        await asyncio.gather(self._command_task, return_exceptions=True)

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
            "market_data_active": self._data_active.is_set(),
            "operator_commands_enabled": self._operator_commands is not None,
            "operator_stop_requested": self._operator_stop_requested.is_set(),
        }

    def _observed_now(self) -> datetime:
        value = self._now()
        require_utc(value, "paper worker observed time")
        return value


def _failure_detail(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}:{message}" if message else type(exc).__name__
