from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from inspect import signature
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from maais.db.connection import Base
from maais.db.models.accounts import AccountSnapshotModel, PositionLotModel, PositionModel
from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.counterfactuals import CounterfactualRepository
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import (
    Direction,
    Disposition,
    GateType,
    PaperOrderSide,
    ProposalStatus,
    ReasonCode,
)
from maais.execution.paper.fills import MarketFillEngine, MarketFillRequest
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.research.counterfactuals import CounterfactualState
from tests.integration.test_decision_lineage import _prepare_bundle

pytestmark = pytest.mark.integration


async def _rejected_state(
    uow_factory: UnitOfWork,
    *,
    no_fill: bool = True,
) -> CounterfactualState:
    manifest, bundle = await _prepare_bundle(uow_factory)
    assert bundle.proposal is not None
    failed_gate = replace(
        bundle.gates[-1],
        passed=False,
        reason_code=ReasonCode.NON_POSITIVE_EV,
        output={"approved": False},
    )
    rejected = replace(
        bundle,
        cycle=replace(
            bundle.cycle,
            disposition=Disposition.REJECTED,
            reason_code=ReasonCode.NON_POSITIVE_EV,
        ),
        gates=(*bundle.gates[:-1], failed_gate),
        proposal=replace(
            bundle.proposal,
            status=ProposalStatus.REJECTED,
            reason_code=ReasonCode.NON_POSITIVE_EV,
        ),
    )
    rejected.validate()
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(rejected)
    state = CounterfactualState.create(
        counterfactual_id=UUID(int=901),
        experiment_id=manifest.experiment_id,
        proposal_id=rejected.proposal.id,
        decision_cycle_id=rejected.cycle.id,
        symbol=rejected.cycle.symbol,
        direction=rejected.cycle.direction,
        rejection_gate=GateType.EV,
        prior_gate_chain=(GateType.DATA_QUALITY, GateType.EV),
        quantity=rejected.proposal.approved_quantity or Decimal("0.001"),
        decision_executable_price=Decimal("60000"),
        eligible_after=rejected.cycle.completed_at + timedelta(milliseconds=100),
        fee_rate=Decimal("0.0005"),
        expected_loss_fraction=rejected.summary.expected_loss,
        expected_gain_fraction=rejected.summary.expected_gain,
        created_at=rejected.cycle.completed_at,
    )
    if no_fill:
        return state.mark_no_fill(
            "insufficient_visible_depth",
            rejected.cycle.completed_at + timedelta(milliseconds=101),
        )
    return state


async def test_counterfactual_schema_has_no_official_account_foreign_keys(
    db_connection: AsyncConnection,
) -> None:
    def inspect_schema(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        table = Base.metadata.tables["counterfactuals"]
        assert {item["name"] for item in inspector.get_columns("counterfactuals")} == {
            column.name for column in table.columns
        }
        referred = {
            item["referred_table"] for item in inspector.get_foreign_keys("counterfactuals")
        }
        assert referred == {"experiments", "trade_proposals", "decision_cycles"}
        assert not referred & {
            "account_snapshots",
            "positions",
            "position_lots",
            "fills",
            "order_intents",
        }

    await db_connection.run_sync(inspect_schema)
    assert list(signature(CounterfactualRepository).parameters) == ["session", "events"]


async def test_counterfactual_record_and_restore_cannot_mutate_official_account(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    state = await _rejected_state(uow_factory)
    async with uow_factory.begin() as uow:
        first = await uow.counterfactuals.record(state)
    async with uow_factory.begin() as uow:
        second = await uow.counterfactuals.record(state)
        restored = await uow.counterfactuals.get(state.counterfactual_id)

    assert first.created
    assert not second.created
    assert restored == state
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CounterfactualModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PositionModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PositionLotModel)) == 0
        official_event_count = await session.scalar(
            select(func.count())
            .select_from(DomainEventModel)
            .where(
                DomainEventModel.aggregate_type.in_(
                    ("paper_account", "paper_order", "paper_fill", "paper_funding")
                )
            )
        )
        assert official_event_count == 0
        event_count = await session.scalar(
            select(func.count())
            .select_from(DomainEventModel)
            .where(
                DomainEventModel.aggregate_type == "counterfactual",
                DomainEventModel.aggregate_id == state.counterfactual_id,
            )
        )
        outbox_count = await session.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .join(DomainEventModel, DomainEventModel.id == OutboxEventModel.domain_event_id)
            .where(
                DomainEventModel.aggregate_type == "counterfactual",
                DomainEventModel.aggregate_id == state.counterfactual_id,
            )
        )
        assert event_count == state.version
        assert outbox_count == state.version


async def test_open_counterfactual_updates_and_resolves_with_exact_restart_state(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    state = await _rejected_state(uow_factory, no_fill=False)
    async with uow_factory.begin() as uow:
        await uow.counterfactuals.record(state)

    observed_at = state.eligible_after + timedelta(milliseconds=1)
    book = BookSnapshot(
        event_id="counterfactual-depth-1",
        symbol=state.symbol,
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=1,
        bids=(BookLevel(Decimal("100"), Decimal("2")),),
        asks=(BookLevel(Decimal("101.5"), Decimal("2")),),
        mark_price=Decimal("100.75"),
    )
    side = PaperOrderSide.BUY if state.direction is Direction.LONG else PaperOrderSide.SELL
    fill = MarketFillEngine(timedelta(seconds=1)).fill(
        MarketFillRequest(
            symbol=state.symbol,
            side=side,
            quantity=state.quantity,
            eligible_after=state.eligible_after,
            decision_executable_price=Decimal("101"),
            taker_fee_rate=state.fee_rate,
        ),
        (book,),
    )
    state = state.enter(fill, plan_id=UUID(int=902))
    async with uow_factory.begin() as uow:
        await uow.counterfactuals.record(state)

    state = state.observe_mark(
        Decimal("101.6"),
        fill.fill_at + timedelta(minutes=15),
        market_event_id="counterfactual-mark-15m",
    )
    async with uow_factory.begin() as uow:
        await uow.counterfactuals.record(state)
    state = state.apply_funding(
        Decimal("0.001"),
        Decimal("101.6"),
        fill.fill_at + timedelta(hours=8),
        market_event_id="counterfactual-funding-8h",
    )
    async with uow_factory.begin() as uow:
        await uow.counterfactuals.record(state)
    state = state.observe_mark(
        Decimal("101.7"),
        fill.fill_at + timedelta(hours=24),
        market_event_id="counterfactual-mark-24h",
    )
    async with uow_factory.begin() as uow:
        await uow.counterfactuals.record(state)
    terminal_mark = Decimal("100") if state.direction is Direction.LONG else Decimal("103")
    state = state.observe_mark(
        terminal_mark,
        fill.fill_at + timedelta(hours=24, seconds=1),
        market_event_id="counterfactual-terminal-mark",
    )
    async with uow_factory.begin() as uow:
        await uow.counterfactuals.record(state)
        restored = await uow.counterfactuals.get(state.counterfactual_id)
        report = await verify_ledger_consistency(uow.session)

    assert restored == state
    assert restored.hypothetical_exit_reason == "stop"
    assert {outcome.horizon for outcome in restored.outcomes} == {"15m", "1h", "4h", "24h"}
    assert report.ok

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PositionModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PositionLotModel)) == 0
