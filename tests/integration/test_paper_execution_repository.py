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
    PositionLotModel,
    PositionModel,
)
from maais.db.models.execution import FillModel, OrderEventModel, OrderIntentModel
from maais.db.models.ledger import DomainEventModel, OutboxEventModel
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
from maais.execution.paper.exits import ExitPlan
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.orders import PaperOrder
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
