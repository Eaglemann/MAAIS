import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID

import pytest

from maais.db.unit_of_work import UnitOfWork
from maais.market_data.events import ObservedMarketEvent
from maais.orchestration.bootstrap import restore_live_paper_runtime
from maais.orchestration.composition import (
    RuntimeAssemblyError,
    assemble_live_paper_application,
)
from maais.orchestration.supervisor import PaperWorkerSupervisorState
from tests.unit.experiments.test_runtime_policy import _live_filter, _live_manifest

pytestmark = pytest.mark.integration
_END = object()


class _PublicData:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.queue.put_nowait(_END)

    async def events(self) -> AsyncGenerator[ObservedMarketEvent, None]:
        while True:
            event = await self.queue.get()
            if event is _END:
                return
            assert isinstance(event, ObservedMarketEvent)
            yield event


@dataclass(frozen=True)
class _Preflight:
    exchange_filters: tuple


class _FuturesRest:
    def __init__(self, preflight: _Preflight) -> None:
        self.result = preflight
        self.calls = 0

    async def preflight(self, required_symbols: tuple[str, ...]) -> _Preflight:
        self.calls += 1
        assert required_symbols == ("BTCUSDT",)
        return self.result

    async def get_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start,
        end,
    ) -> tuple[ObservedMarketEvent, ...]:
        del symbol, interval, start, end
        return ()


async def test_composition_builds_restart_safe_runnable_paper_application(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=301),
        schema_revision="0015",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    snapshot = await restore_live_paper_runtime(uow_factory, manifest)
    pinned = _live_filter()
    current = replace(pinned, captured_at=pinned.captured_at + timedelta(minutes=1))
    futures = _FuturesRest(_Preflight(exchange_filters=(current,)))

    application = await assemble_live_paper_application(
        uow=uow_factory,
        snapshot=snapshot,
        worker_id=UUID(int=302),
        futures_rest=futures,  # type: ignore[arg-type]
        public_data=_PublicData(),  # type: ignore[arg-type]
        signing_key=b"local paper execution key with at least 32 bytes",
        now=lambda: current.captured_at,
    )

    assert futures.calls == 1
    assert application.exchange_filters == {"BTCUSDT": pinned}
    assert application.current_filter_rules_hashes == {"BTCUSDT": current.rules_hash}
    assert application.engine.cursors == {}
    await application.supervisor.start()
    await application.supervisor.stop()
    assert application.supervisor.state is PaperWorkerSupervisorState.STOPPED


async def test_composition_refuses_changed_current_exchange_rules(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=303),
        schema_revision="0015",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    snapshot = await restore_live_paper_runtime(uow_factory, manifest)
    pinned = _live_filter()
    changed = replace(pinned, minimum_notional=pinned.minimum_notional * 2)
    futures = _FuturesRest(_Preflight(exchange_filters=(changed,)))

    with pytest.raises(RuntimeAssemblyError, match="exchange rules changed"):
        await assemble_live_paper_application(
            uow=uow_factory,
            snapshot=snapshot,
            worker_id=UUID(int=304),
            futures_rest=futures,  # type: ignore[arg-type]
            public_data=_PublicData(),  # type: ignore[arg-type]
            signing_key=b"local paper execution key with at least 32 bytes",
            now=lambda: changed.captured_at,
        )
