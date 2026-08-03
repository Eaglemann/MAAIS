from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import PaperOrderSide, PositionEffect
from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import ExecutionAuthorizer
from maais.execution.paper.broker import ExitExecutionHalt, PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.exits import ExitPlan, ExitPlanStatus, ExitReason
from maais.execution.paper.fills import MarketFillEngine
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.operations.operator_commands import CommandType, OperatorCommand
from maais.orchestration.flatten import (
    FlattenSource,
    LivePaperFlattenPlanner,
)
from maais.orchestration.observations import MarketObservationBuffer
from maais.orchestration.operator_control import FlattenPlanningError
from tests.unit.experiments.test_runtime_policy import _live_manifest
from tests.unit.market_data.test_frame_builder import NOW, _book
from tests.unit.orchestration.test_observations import _mark

EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
PROPOSAL_ID = UUID("33333333-3333-4333-8333-333333333333")
POSITION_ID = UUID("44444444-4444-4444-8444-444444444444")
PLAN_ID = UUID("55555555-5555-4555-8555-555555555555")
PLANNED_AT = NOW + timedelta(milliseconds=500)


async def test_flatten_planner_uses_causal_mark_and_strictly_future_reduce_only_fill() -> None:
    manifest = _live_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    policy = LivePaperPolicy.from_manifest(manifest)
    account = AccountState.create(
        EXPERIMENT_ID,
        Decimal("10000"),
        "USDT",
        leverage=1,
    ).apply_fill(
        fill_id=UUID("66666666-6666-4666-8666-666666666666"),
        position_id=POSITION_ID,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.05"),
        fill_at=NOW,
    )
    position = account.position("BTCUSDT")
    exit_plan = ExitPlan.create(
        plan_id=PLAN_ID,
        position_id=POSITION_ID,
        side=position.side,
        quantity=position.quantity,
        average_entry=position.average_entry,
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
    )

    class _Loader:
        async def load(self, command: OperatorCommand) -> FlattenSource:
            assert command.command_id == COMMAND_ID
            return FlattenSource(
                account=account,
                exit_plans=(exit_plan,),
                entry_proposal_ids={POSITION_ID: PROPOSAL_ID},
                pending_order_count=0,
            )

    observations = MarketObservationBuffer(("BTCUSDT",))
    mark = _mark(400)
    boundary_book = _book("book-boundary", 750, "100", "101", 750)
    future_book = _book("book-future", 751, "99.9", "100.1", 751)
    await observations.observe(mark)
    await observations.observe(boundary_book)
    await observations.observe(future_book)
    broker = PaperBroker(
        clock=DeterministicClock(lambda: PLANNED_AT),
        authorizer=ExecutionAuthorizer(b"x" * 32),
        market_fills=MarketFillEngine(policy.integrity_policy().max_book_age),
    )
    command = OperatorCommand.request(
        command_id=COMMAND_ID,
        experiment_id=EXPERIMENT_ID,
        command_type=CommandType.FLATTEN,
        idempotency_key="77777777-7777-4777-8777-777777777777",
        actor="local_operator",
        reason="close all simulated exposure",
        payload={},
        confirmation="CONFIRM FLATTEN",
        requested_at=NOW,
    ).accept(
        accepted_at=NOW + timedelta(milliseconds=100),
        worker_id="paper_worker:88888888-8888-4888-8888-888888888888",
    )

    planned = await LivePaperFlattenPlanner(
        manifest=manifest,
        policy=policy,
        source_loader=_Loader(),
        observations=observations,
        broker=broker,
        exchange_filters=policy.exchange_filters,
        now=lambda: PLANNED_AT,
    ).prepare(command)

    assert planned.source_account == account
    assert planned.planned_at == PLANNED_AT
    assert len(planned.executions) == 1
    execution = planned.executions[0]
    assert execution.order.reduce_only
    assert execution.order.position_effect is PositionEffect.REDUCE
    assert execution.fills[0].market_event_id == "book-future"
    assert execution.account is not None
    assert execution.account.position("BTCUSDT").is_flat
    assert execution.exit_plan is not None
    assert execution.exit_plan.status is ExitPlanStatus.CLOSED
    assert execution.exit_plan.trigger_reason is ExitReason.EMERGENCY
    assert planned.triggers[0].mark_event_id == mark.event_id
    assert planned.triggers[0].mark_observed_at == mark.observed_at
    assert planned.triggers[0].eligible_after == PLANNED_AT + policy.execution_latency


async def test_flatten_planner_converts_unfillable_exit_to_structured_failure() -> None:
    manifest = _live_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    policy = LivePaperPolicy.from_manifest(manifest)
    account = AccountState.create(
        EXPERIMENT_ID,
        Decimal("10000"),
        "USDT",
        leverage=1,
    ).apply_fill(
        fill_id=UUID("66666666-6666-4666-8666-666666666666"),
        position_id=POSITION_ID,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.05"),
        fill_at=NOW,
    )
    position = account.position("BTCUSDT")
    exit_plan = ExitPlan.create(
        plan_id=PLAN_ID,
        position_id=POSITION_ID,
        side=position.side,
        quantity=position.quantity,
        average_entry=position.average_entry,
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
    )

    class _Loader:
        async def load(self, command: OperatorCommand) -> FlattenSource:
            return FlattenSource(
                account=account,
                exit_plans=(exit_plan,),
                entry_proposal_ids={POSITION_ID: PROPOSAL_ID},
                pending_order_count=0,
            )

    class _UnfillableBroker:
        def execute_market_exit(self, command, *, account, exit_plan, books):
            raise ExitExecutionHalt(
                command,
                exit_plan,
                "insufficient_depth",
                market_event_id=books[0].event_id,
            )

    observations = MarketObservationBuffer(("BTCUSDT",))
    await observations.observe(_mark(400))
    await observations.observe(_book("book-future", 751, "99.9", "100.1", 751))
    command = OperatorCommand.request(
        command_id=COMMAND_ID,
        experiment_id=EXPERIMENT_ID,
        command_type=CommandType.FLATTEN,
        idempotency_key="77777777-7777-4777-8777-777777777777",
        actor="local_operator",
        reason="close all simulated exposure",
        payload={},
        confirmation="CONFIRM FLATTEN",
        requested_at=NOW,
    ).accept(
        accepted_at=NOW + timedelta(milliseconds=100),
        worker_id="paper_worker:88888888-8888-4888-8888-888888888888",
    )
    planner = LivePaperFlattenPlanner(
        manifest=manifest,
        policy=policy,
        source_loader=_Loader(),
        observations=observations,
        broker=_UnfillableBroker(),  # type: ignore[arg-type]
        exchange_filters=policy.exchange_filters,
        now=lambda: PLANNED_AT,
    )

    with pytest.raises(FlattenPlanningError) as caught:
        await planner.prepare(command)

    assert caught.value.reason_code == "flatten_exit_unfillable"
    assert "insufficient_depth" in caught.value.detail
    assert "book-future" in caught.value.detail
