from datetime import datetime
from decimal import Decimal
from uuid import UUID

import pytest

from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import PaperOrderType
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import MarketEventKind
from maais.market_data.history import CausalFrameHistory
from maais.monitoring.admission import OfficialAdmissionPolicy
from maais.orchestration.context import (
    LiveEntryContextAssembler,
    TradingControlSnapshot,
)
from maais.orchestration.observations import (
    MarketObservationBuffer,
    RuntimeHealthRegistry,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest
from tests.unit.market_data.test_frame_builder import _book, _inputs
from tests.unit.market_data.test_history import _frame_with_uneven_depth, _history_snapshots

pytestmark = pytest.mark.integration


class _ClearControls:
    def __init__(self, experiment_id: UUID, changed_at: datetime) -> None:
        self._snapshot = TradingControlSnapshot(
            experiment_id=experiment_id,
            kill_switch_active=False,
            reason=None,
            version=0,
            changed_at=changed_at,
        )

    async def current(self, experiment_id: UUID) -> TradingControlSnapshot:
        return self._snapshot


async def test_live_entry_context_restores_account_and_uses_real_future_book(
    uow_factory: UnitOfWork,
) -> None:
    frame = _frame_with_uneven_depth()
    exchange_filter = ExchangeFilterSnapshot(
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("200"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.MARKET,),
        captured_at=frame.cutoff_at,
    )
    manifest = _live_manifest(
        experiment_id=UUID(int=1),
        schema_revision="0012",
        exchange_metadata={
            "venue": "binance_usdm",
            "market": "usdt_perpetual",
            "filter_snapshot_hashes": {"BTCUSDT": exchange_filter.content_hash},
        },
    )
    policy = LivePaperPolicy.from_manifest(manifest)
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    history = CausalFrameHistory(
        manifest.experiment_id,
        manifest.symbols,
        maximum_bars=policy.history_bars,
    )
    history.restore(_history_snapshots(61))
    observations = MarketObservationBuffer(manifest.symbols)
    mark = next(item for item in _inputs() if item.kind is MarketEventKind.MARK_FUNDING)
    await observations.observe(mark)
    await observations.observe(_book("execution-book", 400, "100", "101", 400))
    mandatory = OfficialAdmissionPolicy.conservative().mandatory_components
    health = RuntimeHealthRegistry(mandatory)
    for component in mandatory:
        health.heartbeat(component, frame.cutoff_at)
    assembler = LiveEntryContextAssembler(
        uow=uow_factory,
        manifest=manifest,
        policy=policy,
        history=history,
        observations=observations,
        health=health,
        controls=_ClearControls(manifest.experiment_id, frame.cutoff_at),
        exchange_filters={"BTCUSDT": exchange_filter},
    )

    context = await assembler.build(
        frame,
        evaluated_at=frame.cutoff_at,
        completed_at=frame.cutoff_at,
    )

    assert context.account.equity == manifest.initial_capital
    assert context.account.leverage == 1
    assert context.books[0].event_id == "execution-book"
    assert context.books[0].observed_at > frame.cutoff_at + policy.execution_latency
    assert context.monitoring.volatility is not None
    assert context.monitoring.volatility.sample_count == 60
    assert context.monitoring.benchmark is not None
    assert context.monitoring.benchmark.symbol == "BTCUSDT"
    assert (
        context.monitoring.benchmark.source_event_id
        == frame.source_manifest["primary_spot"].event_id
    )
    assert tuple(item.component for item in context.monitoring.health) == tuple(sorted(mandatory))
    assert not context.monitoring.kill_switch_active
    assert context.open_positions == ()
    assert context.correlations == ()
