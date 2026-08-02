import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.db.unit_of_work import UnitOfWork, UnitOfWorkContext
from maais.domain.enums import ExperimentStatus
from maais.market_data.events import MarketEventKind, ObservedMarketEvent
from maais.orchestration.checkpoints import WorkerLeaseStatus, WorkerStatus
from maais.orchestration.supervisor import (
    PaperWorkerHalt,
    PaperWorkerSupervisor,
    PaperWorkerSupervisorState,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest
from tests.unit.market_data.test_public_runtime import _event

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
_END = object()


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class _PublicData:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.queue.put_nowait(_END)

    async def events(self) -> AsyncGenerator[ObservedMarketEvent, None]:
        while True:
            value = await self.queue.get()
            if value is _END:
                return
            assert isinstance(value, ObservedMarketEvent)
            yield value


class _Observations:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def observe(self, event: ObservedMarketEvent) -> bool:
        self.order.append(f"observe:{event.event_id}")
        return True


class _Engine:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.cursors = {}

    async def process(self, event: ObservedMarketEvent) -> object:
        self.order.append(f"dispatch:{event.event_id}")
        return object()


class _FailingEngine(_Engine):
    async def process(self, event: ObservedMarketEvent) -> object:
        self.order.append(f"dispatch:{event.event_id}")
        raise ArithmeticError("deliberate dispatch failure")


class _AuditFailingSupervisor(PaperWorkerSupervisor):
    async def _audit_recovered_state(self, transaction: UnitOfWorkContext) -> None:
        del transaction
        raise PaperWorkerHalt("startup audit failed")


async def _blocked_sleep(_: float) -> None:
    await asyncio.Event().wait()


async def test_supervisor_owns_lease_checkpoints_orders_events_and_drains(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=77),
        schema_revision="0015",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    order: list[str] = []
    public = _PublicData()
    worker_id = UUID(int=88)
    supervisor = PaperWorkerSupervisor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=worker_id,
        public_data=public,
        observations=_Observations(order),  # type: ignore[arg-type]
        engine=_Engine(order),  # type: ignore[arg-type]
        now=_Clock(),
        sleep=_blocked_sleep,
        lease_ttl=timedelta(seconds=30),
        lease_renew_interval=timedelta(seconds=10),
        drain_timeout=timedelta(seconds=1),
    )

    await supervisor.start()
    event = _event(MarketEventKind.VENUE_CLOCK, "supervised-event")
    public.queue.put_nowait(event)
    for _ in range(100):
        if order == ["observe:supervised-event", "dispatch:supervised-event"]:
            break
        await asyncio.sleep(0)
    await supervisor.stop()

    async with uow_factory.begin() as uow:
        lease = await uow.workers.get(manifest.experiment_id)
        checkpoint = await uow.orchestration.get_checkpoint(manifest.experiment_id)
        control = await uow.controls.current(manifest.experiment_id)
        experiment_status = await uow.experiments.get_status(manifest.experiment_id)

    assert order == ["observe:supervised-event", "dispatch:supervised-event"]
    assert public.started and public.stopped
    assert supervisor.state is PaperWorkerSupervisorState.STOPPED
    assert lease.worker_id == worker_id
    assert lease.status is WorkerLeaseStatus.RELEASED
    assert checkpoint.status is WorkerStatus.STOPPED
    assert tuple(event.event_type for event in checkpoint.events) == (
        "worker_checkpoint.starting",
        "worker_checkpoint.running",
        "worker_checkpoint.stopping",
        "worker_checkpoint.stopped",
    )
    assert not control.kill_switch_active
    assert experiment_status is ExperimentStatus.RUNNING


async def test_supervisor_persists_halt_and_releases_lease_on_dispatch_failure(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=89),
        schema_revision="0015",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    order: list[str] = []
    public = _PublicData()
    worker_id = UUID(int=90)
    supervisor = PaperWorkerSupervisor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=worker_id,
        public_data=public,
        observations=_Observations(order),  # type: ignore[arg-type]
        engine=_FailingEngine(order),  # type: ignore[arg-type]
        now=_Clock(),
        sleep=_blocked_sleep,
        lease_ttl=timedelta(seconds=30),
        lease_renew_interval=timedelta(seconds=10),
        drain_timeout=timedelta(seconds=1),
    )

    await supervisor.start()
    public.queue.put_nowait(_event(MarketEventKind.VENUE_CLOCK, "failed-event"))
    with pytest.raises(PaperWorkerHalt, match="ArithmeticError:deliberate dispatch failure"):
        await supervisor.wait_closed()

    async with uow_factory.begin() as uow:
        lease = await uow.workers.get(manifest.experiment_id)
        checkpoint = await uow.orchestration.get_checkpoint(manifest.experiment_id)
        control = await uow.controls.current(manifest.experiment_id)

    assert order == ["observe:failed-event", "dispatch:failed-event"]
    assert public.stopped
    assert supervisor.state is PaperWorkerSupervisorState.HALTED
    assert lease.status is WorkerLeaseStatus.RELEASED
    assert checkpoint.status is WorkerStatus.HALTED
    assert checkpoint.events[-1].event_type == "worker_checkpoint.halted"
    assert control.kill_switch_active
    assert control.reason is not None
    assert "ArithmeticError:deliberate dispatch failure" in control.reason


async def test_supervisor_persists_startup_audit_failure_before_public_data(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=91),
        schema_revision="0015",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    public = _PublicData()
    supervisor = _AuditFailingSupervisor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=UUID(int=92),
        public_data=public,
        observations=_Observations([]),  # type: ignore[arg-type]
        engine=_Engine([]),  # type: ignore[arg-type]
        now=_Clock(),
        sleep=_blocked_sleep,
        lease_ttl=timedelta(seconds=30),
        lease_renew_interval=timedelta(seconds=10),
        drain_timeout=timedelta(seconds=1),
    )

    with pytest.raises(PaperWorkerHalt, match="startup audit failed"):
        await supervisor.start()

    async with uow_factory.begin() as uow:
        lease = await uow.workers.get(manifest.experiment_id)
        checkpoint = await uow.orchestration.get_checkpoint(manifest.experiment_id)
        control = await uow.controls.current(manifest.experiment_id)

    assert not public.started
    assert supervisor.state is PaperWorkerSupervisorState.HALTED
    assert lease.status is WorkerLeaseStatus.RELEASED
    assert checkpoint.status is WorkerStatus.HALTED
    assert control.kill_switch_active
    assert control.reason is not None
    assert "startup audit failed" in control.reason
