import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from maais.db.connection import Base
from maais.db.models.accounts import (
    AccountSnapshotModel,
    ExitPlanModel,
    FundingEntryModel,
    PositionLotModel,
    PositionModel,
)
from maais.db.models.execution import (
    ExecutionSensitivityModel,
    FillModel,
    OrderEventModel,
    OrderIntentModel,
)
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.execution import (
    ExecutionFillRecord,
    OrderIdentityConflict,
    PaperExecutionRecord,
)
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import (
    PaperOrderSide,
    PaperOrderStatus,
    PaperOrderType,
    PositionEffect,
)
from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import ExecutionAuthorizer
from maais.execution.paper.broker import MarketExitCommand, PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.exits import ExitPlan
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.execution.paper.orders import PaperOrder
from maais.execution.paper.records import FundingRecord
from maais.execution.paper.sensitivity import SensitivityOutcome, SensitivityScenario
from tests.integration.test_decision_lineage import _prepare_bundle

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
_TABLES = (
    "order_intents",
    "order_events",
    "fills",
    "positions",
    "position_lots",
    "exit_plans",
    "account_snapshots",
    "funding_entries",
    "execution_sensitivities",
)


async def test_paper_schema_matches_model_columns_and_keys(
    db_connection: AsyncConnection,
) -> None:
    def compare(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        for table_name in _TABLES:
            table = Base.metadata.tables[table_name]
            migrated = {column["name"]: column for column in inspector.get_columns(table_name)}
            assert set(migrated) == {column.name for column in table.columns}
            assert set(inspector.get_pk_constraint(table_name)["constrained_columns"]) == {
                column.name for column in table.primary_key.columns
            }
            assert {
                tuple(item["constrained_columns"])
                for item in inspector.get_foreign_keys(table_name)
            } == {tuple(item.column_keys) for item in table.foreign_key_constraints}

    await db_connection.run_sync(compare)


async def _record(uow_factory: UnitOfWork) -> PaperExecutionRecord:
    manifest, decision = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(decision)
    assert decision.proposal is not None
    fill_id = UUID(int=501)
    position_id = UUID(int=601)
    order = PaperOrder.create(
        order_id=UUID(int=401),
        experiment_id=manifest.experiment_id,
        proposal_id=decision.proposal.id,
        client_order_id="paper-btc-1",
        command_hash="c" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        order_type=PaperOrderType.MARKET,
        position_effect=PositionEffect.OPEN,
        quantity=Decimal("0.1"),
        limit_price=None,
        reduce_only=False,
        open_quantity=Decimal("0"),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    order = order.authorize(NOW + timedelta(milliseconds=1)).accept(NOW + timedelta(milliseconds=2))
    order = order.apply_fill(Decimal("0.1"), NOW + timedelta(milliseconds=3))
    fill = ExecutionFillRecord(
        id=fill_id,
        order_intent_id=order.order_id,
        market_event_id="depth-100",
        fill_at=NOW + timedelta(milliseconds=3),
        quantity=Decimal("0.1"),
        price=Decimal("60000"),
        liquidity_role="taker",
        fee=Decimal("3"),
        fee_asset="USDT",
        spread_cost=Decimal("0.05"),
        depth_slippage=Decimal("0"),
        latency_slippage=Decimal("0.02"),
        total_slippage=Decimal("0.07"),
        market_snapshot={"best_ask": "60000", "sequence": 100},
    )
    account = AccountState.create(
        manifest.experiment_id,
        Decimal("10000"),
        "USDT",
        leverage=1,
    ).apply_fill(
        fill_id=fill_id,
        position_id=position_id,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=fill.quantity,
        price=fill.price,
        fee=fill.fee,
        fill_at=fill.fill_at,
    )
    position = account.position("BTCUSDT")
    exit_plan = ExitPlan.create(
        plan_id=UUID(int=701),
        position_id=position_id,
        side=position.side,
        quantity=position.quantity,
        average_entry=position.average_entry,
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=fill.fill_at,
    )
    filters = ExchangeFilterSnapshot(
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("10"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.MARKET, PaperOrderType.LIMIT),
        captured_at=NOW - timedelta(minutes=1),
    )
    return PaperExecutionRecord(order, filters, (fill,), account, exit_plan)


async def test_execution_account_and_events_commit_atomically_and_restore(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    record = await _record(uow_factory)
    async with uow_factory.begin() as uow:
        result = await uow.paper_execution.record(record)

    assert result.created
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderIntentModel)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderEventModel)) == 4
        assert await session.scalar(select(func.count()).select_from(FillModel)) == 1
        assert await session.scalar(select(func.count()).select_from(PositionModel)) == 1
        assert await session.scalar(select(func.count()).select_from(PositionLotModel)) == 1
        assert await session.scalar(select(func.count()).select_from(ExitPlanModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 1
        domain_count = int(
            await session.scalar(select(func.count()).select_from(DomainEventModel)) or 0
        )
        outbox_count = int(
            await session.scalar(select(func.count()).select_from(OutboxEventModel)) or 0
        )
        assert domain_count == outbox_count

    async with uow_factory.begin() as uow:
        restored = await uow.paper_execution.load_account(record.account.experiment_id)
    assert restored.snapshot() == record.account.snapshot()
    assert restored.position("BTCUSDT") == record.account.position("BTCUSDT")
    assert restored.reconcile().ok


async def test_identical_execution_retry_is_idempotent_and_changed_command_conflicts(
    uow_factory: UnitOfWork,
) -> None:
    record = await _record(uow_factory)
    async with uow_factory.begin() as uow:
        first = await uow.paper_execution.record(record)
    async with uow_factory.begin() as uow:
        second = await uow.paper_execution.record(record)

    assert first.created
    assert not second.created
    changed = replace(record, order=replace(record.order, command_hash="d" * 64))
    with pytest.raises(OrderIdentityConflict):
        async with uow_factory.begin() as uow:
            await uow.paper_execution.record(changed)


async def test_execution_transaction_rolls_back_all_projections(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    record = await _record(uow_factory)
    with pytest.raises(RuntimeError, match="rollback"):
        async with uow_factory.begin() as uow:
            await uow.paper_execution.record(record)
            raise RuntimeError("rollback")

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderIntentModel)) == 0
        assert await session.scalar(select(func.count()).select_from(FillModel)) == 0
        assert await session.scalar(select(func.count()).select_from(PositionModel)) == 0


async def test_pending_order_advances_through_two_partial_fills_exactly_once(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    final_record = await _record(uow_factory)
    accepted = replace(
        final_record.order,
        filled_quantity=Decimal("0"),
        status=PaperOrderStatus.ACCEPTED,
        version=3,
        events=final_record.order.events[:3],
    )
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(
            PaperExecutionRecord(
                accepted,
                final_record.exchange_filters,
                (),
                None,
                None,
            )
        )
    async with uow_factory.begin() as uow:
        pending_orders = await uow.paper_execution.load_pending_orders(accepted.experiment_id)

    assert len(pending_orders) == 1
    assert pending_orders[0].order == accepted
    assert pending_orders[0].exchange_filters == final_record.exchange_filters

    first_order = accepted.apply_fill(Decimal("0.04"), NOW + timedelta(milliseconds=3))
    first_fill = replace(
        final_record.fills[0],
        id=UUID(int=501),
        market_event_id="depth-100-a",
        quantity=Decimal("0.04"),
        fee=Decimal("1.2"),
    )
    first_account = AccountState.create(
        first_order.experiment_id,
        Decimal("10000"),
        "USDT",
        leverage=1,
    ).apply_fill(
        fill_id=first_fill.id,
        position_id=UUID(int=601),
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=first_fill.quantity,
        price=first_fill.price,
        fee=first_fill.fee,
        fill_at=first_fill.fill_at,
    )
    first_position = first_account.position("BTCUSDT")
    first_plan = ExitPlan.create(
        plan_id=UUID(int=701),
        position_id=first_position.position_id,
        side=first_position.side,
        quantity=first_position.quantity,
        average_entry=first_position.average_entry,
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=first_fill.fill_at,
    )
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(
            PaperExecutionRecord(
                first_order,
                final_record.exchange_filters,
                (first_fill,),
                first_account,
                first_plan,
            )
        )

    second_order = first_order.apply_fill(Decimal("0.06"), NOW + timedelta(milliseconds=4))
    second_fill = replace(
        final_record.fills[0],
        id=UUID(int=502),
        market_event_id="depth-100-b",
        fill_at=NOW + timedelta(milliseconds=4),
        quantity=Decimal("0.06"),
        fee=Decimal("1.8"),
    )
    second_account = first_account.apply_fill(
        fill_id=second_fill.id,
        position_id=UUID(int=601),
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=second_fill.quantity,
        price=second_fill.price,
        fee=second_fill.fee,
        fill_at=second_fill.fill_at,
    )
    second_position = second_account.position("BTCUSDT")
    second_plan = first_plan.resize(
        quantity=second_position.quantity,
        average_entry=second_position.average_entry,
        changed_at=second_fill.fill_at,
    )
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(
            PaperExecutionRecord(
                second_order,
                final_record.exchange_filters,
                (first_fill, second_fill),
                second_account,
                second_plan,
            )
        )
    async with uow_factory.begin() as uow:
        assert await uow.paper_execution.load_pending_orders(accepted.experiment_id) == ()
        restored_account = await uow.paper_execution.load_account(accepted.experiment_id)
        open_exit_plans = await uow.paper_execution.load_open_exit_plans(accepted.experiment_id)

    assert restored_account == second_account
    assert open_exit_plans == (second_plan,)

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderIntentModel)) == 1
        assert await session.scalar(select(func.count()).select_from(OrderEventModel)) == 5
        assert await session.scalar(select(func.count()).select_from(FillModel)) == 2
        assert await session.scalar(select(func.count()).select_from(PositionLotModel)) == 2
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 2


async def test_concurrent_identical_execution_has_one_creator(
    uow_factory: UnitOfWork,
) -> None:
    record = await _record(uow_factory)

    async def persist():
        async with uow_factory.begin() as uow:
            return await uow.paper_execution.record(record)

    results = await asyncio.gather(persist(), persist())

    assert sorted(item.created for item in results) == [False, True]


async def test_sensitivity_records_are_immutable_and_do_not_change_account(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    record = await _record(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(record)
    calculated_at = NOW + timedelta(minutes=15)
    outcomes = tuple(
        SensitivityOutcome(
            scenario=scenario,
            calculated_at=calculated_at,
            effective_fill_price=Decimal(price),
            fee=Decimal("3"),
            execution_cost=Decimal(cost),
            marked_pnl=Decimal(pnl),
        )
        for scenario, price, cost, pnl in (
            (SensitivityScenario.OPTIMISTIC, "59990", "3", "7"),
            (SensitivityScenario.CONSERVATIVE, "60000", "4", "6"),
            (SensitivityScenario.STRESS, "60020", "6", "4"),
        )
    )
    async with uow_factory.begin() as uow:
        assert await uow.paper_execution.record_sensitivities(record.order.order_id, outcomes) == 3
    async with uow_factory.begin() as uow:
        assert await uow.paper_execution.record_sensitivities(record.order.order_id, outcomes) == 0

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ExecutionSensitivityModel)) == 3
        )
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 1


async def test_observed_funding_persists_with_reconciled_account_snapshot(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    record = await _record(uow_factory)
    assert record.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(record)
    funding_at = NOW + timedelta(hours=8)
    rate = Decimal("0.001")
    position = record.account.position("BTCUSDT")
    notional = position.gross_notional
    funded = record.account.apply_funding(
        "BTCUSDT",
        rate=rate,
        observed_at=funding_at,
    )
    funding = FundingRecord(
        id=UUID(int=801),
        experiment_id=record.account.experiment_id,
        position_id=position.position_id,
        market_event_id="funding-2026-08-02T20:00:00Z",
        observed_at=funding_at,
        rate=rate,
        notional=notional,
        amount=-notional * rate,
    )
    async with uow_factory.begin() as uow:
        assert await uow.paper_execution.record_funding(funding, funded)
    async with uow_factory.begin() as uow:
        assert not await uow.paper_execution.record_funding(funding, funded)
        restored = await uow.paper_execution.load_account(record.account.experiment_id)

    assert restored.snapshot() == funded.snapshot()
    assert restored.reconcile().ok
    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(FundingEntryModel)) == 1
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 2


async def test_entry_funding_stop_gap_exit_and_restart_reconstruct_exactly(
    uow_factory: UnitOfWork,
    db_engine: AsyncEngine,
) -> None:
    entry = await _record(uow_factory)
    assert entry.account is not None
    assert entry.exit_plan is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(entry)

    funding_at = NOW + timedelta(hours=8)
    position = entry.account.position("BTCUSDT")
    funding_rate = Decimal("0.001")
    funded = entry.account.apply_funding(
        "BTCUSDT",
        rate=funding_rate,
        observed_at=funding_at,
    )
    funding = FundingRecord(
        id=UUID(int=801),
        experiment_id=entry.account.experiment_id,
        position_id=position.position_id,
        market_event_id="funding-full-sequence",
        observed_at=funding_at,
        rate=funding_rate,
        notional=position.gross_notional,
        amount=-position.gross_notional * funding_rate,
    )
    async with uow_factory.begin() as uow:
        assert await uow.paper_execution.record_funding(funding, funded)

    trigger_at = funding_at + timedelta(seconds=1)
    triggered = entry.exit_plan.evaluate_mark(entry.exit_plan.stop_price, trigger_at)
    assert triggered.intent is not None
    book_at = trigger_at + timedelta(milliseconds=101)
    exit_book = BookSnapshot(
        event_id="stop-gap-depth-1",
        symbol="BTCUSDT",
        venue_event_at=book_at - timedelta(milliseconds=1),
        observed_at=book_at,
        sequence=901,
        bids=(BookLevel(Decimal("59000"), Decimal("1")),),
        asks=(BookLevel(Decimal("59001"), Decimal("1")),),
        mark_price=Decimal("59000.5"),
    )
    broker = PaperBroker(
        clock=DeterministicClock(lambda: trigger_at),
        authorizer=ExecutionAuthorizer(b"paper exit integration key is at least 32 bytes"),
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )
    exit_result = broker.execute_market_exit(
        MarketExitCommand(
            order_id=UUID(int=402),
            fill_id=UUID(int=502),
            experiment_id=entry.account.experiment_id,
            proposal_id=entry.order.proposal_id,
            client_order_id="paper-btc-stop-1",
            symbol="BTCUSDT",
            decision_executable_price=entry.exit_plan.stop_price,
            execution_latency=timedelta(milliseconds=100),
            created_at=trigger_at,
            expires_at=trigger_at + timedelta(seconds=30),
            taker_fee_rate=Decimal("0.0005"),
            intent=triggered.intent,
            exchange_filters=entry.exchange_filters,
        ),
        account=funded,
        exit_plan=triggered.plan,
        books=(exit_book,),
    )
    assert exit_result.record.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(exit_result.record)
    async with uow_factory.begin() as uow:
        restored = await uow.paper_execution.load_account(entry.account.experiment_id)
        report = await verify_ledger_consistency(uow.session)

    assert exit_result.fill.price == Decimal("59000")
    assert restored.snapshot() == exit_result.record.account.snapshot()
    assert restored.position("BTCUSDT").is_flat
    assert restored.cash_balance == Decimal("9888.05000")
    assert restored.reconcile().ok
    assert report.ok

    factory = async_sessionmaker(db_engine)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderIntentModel)) == 2
        assert await session.scalar(select(func.count()).select_from(FillModel)) == 2
        assert await session.scalar(select(func.count()).select_from(AccountSnapshotModel)) == 3
        exit_plan = await session.get(ExitPlanModel, entry.exit_plan.plan_id)
        assert exit_plan is not None
        assert exit_plan.status == "closed"
