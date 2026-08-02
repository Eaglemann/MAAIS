import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from maais.db.models.accounts import ExitPlanModel
from maais.db.models.execution import FillModel, OrderIntentModel
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import Direction
from maais.execution.paper.exits import ExitPlanStatus
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import PriceLevel
from maais.orchestration.continuous import ContinuousPaperObserver
from maais.orchestration.observations import MarketObservationBuffer
from maais.orchestration.protection import PositionProtectionService
from tests.integration.test_orchestration_repository import (
    _command_in_database,
    _start_experiment,
)
from tests.unit.market_data.test_frame_builder import _book
from tests.unit.orchestration.test_protection import _broker, _mark
from tests.unit.orchestration.test_service import (
    _execution_service,
    _FeatureComputer,
    _features,
)

pytestmark = pytest.mark.integration


def _policy(command) -> LivePaperPolicy:
    assert command.entry_context is not None
    return LivePaperPolicy(
        proposal_ttl=command.entry_context.proposal_ttl,
        book_wait_timeout=timedelta(seconds=1),
        execution_latency=command.entry_context.execution_latency,
        maximum_decision_lag=timedelta(seconds=5),
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=command.entry_context.taker_fee_rate,
        leverage=1,
        history_bars=240,
        benchmark_symbol="BTCUSDT",
        benchmark_horizon_bars=60,
        benchmark_source="binance_spot_close",
        exchange_filter_hashes={"BTCUSDT": command.entry_context.exchange_filters.content_hash},
    )


def _event_book(event_id: str, observed_at, bid: str, ask: str, sequence: int):
    event = _book(event_id, 0, bid, ask, sequence)
    payload = replace(
        event.payload,
        published_at=observed_at - timedelta(milliseconds=1),
        bids=(PriceLevel(Decimal(bid), Decimal("200")),),
        asks=(PriceLevel(Decimal(ask), Decimal("200")),),
    )
    return replace(
        event,
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        payload=payload,
    )


async def _runtime_with_open_position(
    uow_factory: UnitOfWork,
    *,
    book_wait_timeout: timedelta = timedelta(seconds=1),
):
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
    )
    await _start_experiment(uow_factory, command)
    assert command.entry_context is not None
    async with uow_factory.begin() as uow:
        await uow.controls.initialize(
            command.manifest.experiment_id,
            initialized_at=command.evaluated_at,
            actor="paper_worker",
        )
    entry = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(command)
    assert entry.execution is not None
    assert entry.execution.account is not None
    assert entry.execution.exit_plan is not None
    policy = replace(_policy(command), book_wait_timeout=book_wait_timeout)
    async with uow_factory.begin() as uow:
        await uow.orchestration.record_outcome(
            entry,
            integrity=command.integrity,
            required_checks=policy.integrity_policy().required_checks,
            evaluated_at=command.evaluated_at,
        )
    observations = MarketObservationBuffer(command.manifest.symbols)
    observer = ContinuousPaperObserver(
        uow=uow_factory,
        manifest=command.manifest,
        policy=policy,
        observations=observations,
        protection=PositionProtectionService(_broker()),
        exchange_filters={
            "BTCUSDT": command.entry_context.exchange_filters,
        },
    )
    return command, entry, observations, observer


async def test_stop_waits_for_later_book_and_persists_restart_safe_exit(
    uow_factory: UnitOfWork,
) -> None:
    command, entry, observations, observer = await _runtime_with_open_position(uow_factory)
    assert command.entry_context is not None
    assert entry.execution is not None
    assert entry.execution.exit_plan is not None
    trigger_at = command.completed_at + timedelta(seconds=1)
    prior_mark = _mark("mark-prior", Decimal("100"), trigger_at - timedelta(milliseconds=1))
    prior_book = _event_book("book-at-trigger", trigger_at, "98", "99", 1_000)
    trigger = _mark(
        "mark-stop",
        entry.execution.exit_plan.stop_price,
        trigger_at,
    )
    await observations.observe(prior_mark)
    await observations.observe(prior_book)
    await observations.observe(trigger)

    observing = asyncio.create_task(observer.observe(trigger, context_events=()))
    await asyncio.sleep(0)
    future_at = trigger_at + command.entry_context.execution_latency + timedelta(milliseconds=1)
    future_book = _event_book("book-after-latency", future_at, "95", "96", 1_001)
    await observations.observe(future_book)
    await observing

    async with uow_factory.begin() as uow:
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)
        plans = await uow.paper_execution.load_open_exit_plans(command.manifest.experiment_id)
        control = await uow.controls.current(command.manifest.experiment_id)
        exit_fill_event = await uow.session.scalar(
            select(FillModel.market_event_id)
            .join(OrderIntentModel, OrderIntentModel.id == FillModel.order_intent_id)
            .where(OrderIntentModel.position_effect == "reduce")
        )
        persisted_plan = await uow.session.get(
            ExitPlanModel,
            entry.execution.exit_plan.plan_id,
        )

    assert account.position("BTCUSDT").is_flat
    assert account.updated_at == future_at
    assert plans == ()
    assert exit_fill_event == "book-after-latency"
    assert persisted_plan is not None
    assert persisted_plan.trigger_executable_price == Decimal("98")
    assert not control.kill_switch_active


async def test_stop_without_later_book_halts_instead_of_inventing_fill(
    uow_factory: UnitOfWork,
) -> None:
    command, entry, observations, observer = await _runtime_with_open_position(
        uow_factory,
        book_wait_timeout=timedelta(milliseconds=1),
    )
    assert entry.execution is not None
    assert entry.execution.exit_plan is not None
    trigger_at = command.completed_at + timedelta(seconds=1)
    prior_mark = _mark("mark-prior-timeout", Decimal("100"), trigger_at - timedelta(milliseconds=1))
    prior_book = _event_book("book-trigger-timeout", trigger_at, "98", "99", 2_000)
    trigger = _mark(
        "mark-stop-timeout",
        entry.execution.exit_plan.stop_price,
        trigger_at,
    )
    await observations.observe(prior_mark)
    await observations.observe(prior_book)
    await observations.observe(trigger)

    await observer.observe(trigger, context_events=())

    async with uow_factory.begin() as uow:
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)
        plans = await uow.paper_execution.load_open_exit_plans(command.manifest.experiment_id)
        incidents = await uow.incidents.get_unresolved(command.manifest.experiment_id)
        control = await uow.controls.current(command.manifest.experiment_id)
        exit_fill_event = await uow.session.scalar(
            select(FillModel.market_event_id)
            .join(OrderIntentModel, OrderIntentModel.id == FillModel.order_intent_id)
            .where(OrderIntentModel.position_effect == "reduce")
        )

    assert not account.position("BTCUSDT").is_flat
    assert len(plans) == 1 and plans[0].status is ExitPlanStatus.TRIGGERED
    assert len(incidents) == 1
    assert incidents[0].reason_code == "protective_exit_unfillable"
    assert control.kill_switch_active
    assert control.reason is not None and control.reason.startswith("position_protection:")
    assert exit_fill_event is None
