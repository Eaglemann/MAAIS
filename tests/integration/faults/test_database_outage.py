from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.db.unit_of_work import UnitOfWork, UnitOfWorkContext
from maais.orchestration.checkpoints import WorkerLeaseStatus, WorkerStatus
from maais.orchestration.supervisor import (
    PaperWorkerHalt,
    PaperWorkerSupervisor,
    PaperWorkerSupervisorState,
)
from tests.integration.test_paper_worker_supervisor import (
    _blocked_sleep,
    _Engine,
    _Observations,
    _PublicData,
    _SleepControl,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest

pytestmark = pytest.mark.integration
NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(microseconds=1)
        return value


class _OutageUnitOfWork:
    def __init__(self, delegate: UnitOfWork) -> None:
        self.delegate = delegate
        self.unavailable = False

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[UnitOfWorkContext]:
        if self.unavailable:
            raise ConnectionError("database unavailable")
        async with self.delegate.begin() as transaction:
            yield transaction


async def test_database_outage_halts_before_more_work_and_expired_lease_restarts_cleanly(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(experiment_id=UUID(int=1001), schema_revision="0015")
    async with uow_factory.begin() as transaction:
        await transaction.experiments.create(manifest)

    outage_uow = _OutageUnitOfWork(uow_factory)
    sleep = _SleepControl()
    first_public = _PublicData()
    first = PaperWorkerSupervisor(
        uow=outage_uow,  # type: ignore[arg-type]
        manifest=manifest,
        worker_id=UUID(int=1002),
        public_data=first_public,
        observations=_Observations([]),  # type: ignore[arg-type]
        engine=_Engine([]),  # type: ignore[arg-type]
        now=_Clock(NOW),
        sleep=sleep,
        lease_ttl=timedelta(seconds=30),
        lease_renew_interval=timedelta(seconds=10),
        drain_timeout=timedelta(seconds=1),
    )
    await first.start()

    outage_uow.unavailable = True
    sleep.release(10)
    with pytest.raises(
        PaperWorkerHalt,
        match=(
            "ConnectionError:database unavailable; "
            "halt persistence failed: ConnectionError:database unavailable"
        ),
    ):
        await first.wait_closed()

    assert first.state is PaperWorkerSupervisorState.HALTED
    assert first_public.stopped

    outage_uow.unavailable = False
    second_public = _PublicData()
    second = PaperWorkerSupervisor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=UUID(int=1003),
        public_data=second_public,
        observations=_Observations([]),  # type: ignore[arg-type]
        engine=_Engine([]),  # type: ignore[arg-type]
        now=_Clock(NOW + timedelta(seconds=31)),
        sleep=_blocked_sleep,
        lease_ttl=timedelta(seconds=30),
        lease_renew_interval=timedelta(seconds=10),
        drain_timeout=timedelta(seconds=1),
    )
    await second.start()
    await second.stop()

    async with uow_factory.begin() as transaction:
        lease = await transaction.workers.get(manifest.experiment_id)
        checkpoint = await transaction.orchestration.get_checkpoint(manifest.experiment_id)

    assert lease.epoch == 2
    assert lease.status is WorkerLeaseStatus.RELEASED
    assert checkpoint.worker_id == UUID(int=1003)
    assert checkpoint.status is WorkerStatus.STOPPED
