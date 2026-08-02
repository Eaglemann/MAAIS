import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from maais.db.models.accounts import ExitPlanModel, FundingEntryModel
from maais.db.models.execution import FillModel, OrderIntentModel
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import Direction
from maais.execution.paper.exits import ExitPlanStatus
from maais.execution.paper.fills import MarketFillEngine
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import (
    FundingSettlementPayload,
    MarketEventKind,
    ObservedMarketEvent,
    PriceLevel,
)
from maais.orchestration.continuous import ContinuousPaperObserver, ContinuousRuntimeConflict
from maais.orchestration.observations import MarketObservationBuffer
from maais.orchestration.protection import PositionProtectionService
from maais.orchestration.results import OrchestrationDisposition
from maais.research.counterfactuals import CounterfactualStatus
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
        strategy_key="maais_primary",
        strategy_version="1.0.0",
        strategy_implementation_hash="b" * 64,
        strategy_parameters={"timeframe": "1m"},
        exchange_filter_hashes={"BTCUSDT": command.entry_context.exchange_filters.content_hash},
        exchange_filters={"BTCUSDT": command.entry_context.exchange_filters},
    )


def _event_book(
    event_id: str,
    observed_at,
    bid: str,
    ask: str,
    sequence: int,
    *,
    quantity: str = "200",
):
    event = _book(event_id, 0, bid, ask, sequence)
    payload = replace(
        event.payload,
        published_at=observed_at - timedelta(milliseconds=1),
        bids=(PriceLevel(Decimal(bid), Decimal(quantity)),),
        asks=(PriceLevel(Decimal(ask), Decimal(quantity)),),
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
        market_fills=MarketFillEngine(timedelta(seconds=1)),
        exchange_filters={
            "BTCUSDT": command.entry_context.exchange_filters,
        },
    )
    return command, entry, observations, observer


async def _runtime_with_pending_counterfactual(uow_factory: UnitOfWork):
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
        kill_switch=True,
    )
    assert command.entry_context is not None
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.disposition is OrchestrationDisposition.REJECTED
    assert outcome.counterfactual is not None
    policy = _policy(command)
    async with uow_factory.begin() as uow:
        await uow.orchestration.record_outcome(
            outcome,
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
        market_fills=MarketFillEngine(timedelta(seconds=1)),
        exchange_filters={"BTCUSDT": command.entry_context.exchange_filters},
    )
    return command, outcome, observations, observer


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


async def test_observed_funding_settlement_is_applied_exactly_once_after_restart(
    uow_factory: UnitOfWork,
) -> None:
    command, entry, _, observer = await _runtime_with_open_position(uow_factory)
    assert entry.execution is not None
    assert entry.execution.account is not None
    funding_at = command.completed_at + timedelta(hours=8)
    observed_at = funding_at + timedelta(milliseconds=50)
    event = ObservedMarketEvent(
        venue="binance_usdm",
        stream="rest:/fapi/v1/fundingRate",
        symbol="BTCUSDT",
        event_id="binance_usdm:funding:BTCUSDT:1:Regular",
        kind=MarketEventKind.FUNDING_SETTLEMENT,
        venue_event_at=funding_at,
        observed_at=observed_at,
        sequence=None,
        sequence_not_applicable_reason="binance_funding_history_has_no_sequence",
        payload=FundingSettlementPayload(
            funding_at=funding_at,
            funding_rate=Decimal("0.001"),
            mark_price=Decimal("101"),
            rate_type="Regular",
        ),
    )

    await observer.observe(event, context_events=())
    await observer.observe(event, context_events=())
    with pytest.raises(ContinuousRuntimeConflict, match="different persisted content"):
        await observer.observe(
            replace(
                event,
                payload=replace(event.payload, mark_price=Decimal("102")),
            ),
            context_events=(),
        )

    async with uow_factory.begin() as uow:
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)
        funding_rows = (
            await uow.session.scalars(
                select(FundingEntryModel).where(
                    FundingEntryModel.experiment_id == command.manifest.experiment_id
                )
            )
        ).all()

    assert len(funding_rows) == 1
    assert funding_rows[0].market_event_id == event.event_id
    assert funding_rows[0].mark_price == Decimal("101")
    assert account.updated_at == observed_at
    assert account.funding == funding_rows[0].amount
    assert account.version == entry.execution.account.version + 2


async def test_funding_before_position_open_is_not_applied_retroactively(
    uow_factory: UnitOfWork,
) -> None:
    command, entry, _, observer = await _runtime_with_open_position(uow_factory)
    assert entry.execution is not None
    assert entry.execution.account is not None
    position = entry.execution.account.position("BTCUSDT")
    assert position.opened_at is not None
    funding_at = position.opened_at - timedelta(hours=1)
    event = ObservedMarketEvent(
        venue="binance_usdm",
        stream="rest:/fapi/v1/fundingRate",
        symbol="BTCUSDT",
        event_id="binance_usdm:funding:BTCUSDT:before-open:Regular",
        kind=MarketEventKind.FUNDING_SETTLEMENT,
        venue_event_at=funding_at,
        observed_at=position.opened_at + timedelta(seconds=1),
        sequence=None,
        sequence_not_applicable_reason="binance_funding_history_has_no_sequence",
        payload=FundingSettlementPayload(
            funding_at=funding_at,
            funding_rate=Decimal("0.001"),
            mark_price=Decimal("101"),
            rate_type="Regular",
        ),
    )

    await observer.observe(event, context_events=(event,))
    await observer.observe(event, context_events=(event,))

    async with uow_factory.begin() as uow:
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)
        funding_rows = (
            await uow.session.scalars(
                select(FundingEntryModel).where(
                    FundingEntryModel.experiment_id == command.manifest.experiment_id
                )
            )
        ).all()

    assert account == entry.execution.account
    assert funding_rows == []


async def test_first_eligible_book_opens_counterfactual_without_touching_official_account(
    uow_factory: UnitOfWork,
) -> None:
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
        kill_switch=True,
    )
    assert command.entry_context is not None
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.disposition is OrchestrationDisposition.REJECTED
    assert outcome.counterfactual is not None
    policy = _policy(command)
    async with uow_factory.begin() as uow:
        await uow.orchestration.record_outcome(
            outcome,
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
        market_fills=MarketFillEngine(timedelta(seconds=1)),
        exchange_filters={
            "BTCUSDT": command.entry_context.exchange_filters,
        },
    )
    eligible_after = outcome.counterfactual.eligible_after
    mark = _mark("counterfactual-mark", Decimal("100"), eligible_after)
    book = _event_book(
        "counterfactual-entry-book",
        eligible_after + timedelta(milliseconds=1),
        "100",
        "101",
        3_000,
    )
    await observations.observe(mark)
    await observations.observe(book)

    await observer.observe(book, context_events=(mark, book))
    await observer.observe(book, context_events=(mark, book))

    async with uow_factory.begin() as uow:
        unresolved = await uow.counterfactuals.get_unresolved(command.manifest.experiment_id)
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)

    assert len(unresolved) == 1
    counterfactual = unresolved[0]
    assert counterfactual.status is CounterfactualStatus.OPEN
    assert counterfactual.entry_fill is not None
    assert counterfactual.entry_fill.market_event_id == book.event_id
    assert counterfactual.entry_fill.price == Decimal("101")
    assert (
        counterfactual.decision_executable_price == outcome.counterfactual.decision_executable_price
    )
    assert account.version == 0
    assert account.positions == {}


async def test_counterfactual_marks_and_funding_advance_once_without_official_position(
    uow_factory: UnitOfWork,
) -> None:
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
        kill_switch=True,
    )
    assert command.entry_context is not None
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.counterfactual is not None
    policy = _policy(command)
    async with uow_factory.begin() as uow:
        await uow.orchestration.record_outcome(
            outcome,
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
        market_fills=MarketFillEngine(timedelta(seconds=1)),
        exchange_filters={"BTCUSDT": command.entry_context.exchange_filters},
    )
    eligible_after = outcome.counterfactual.eligible_after
    entry_mark = _mark("counterfactual-entry-mark", Decimal("100"), eligible_after)
    entry_book = _event_book(
        "counterfactual-lifecycle-entry",
        eligible_after + timedelta(milliseconds=1),
        "100",
        "101",
        4_000,
    )
    await observations.observe(entry_mark)
    await observations.observe(entry_book)
    await observer.observe(entry_book, context_events=(entry_mark, entry_book))

    mark_at = entry_book.observed_at + timedelta(minutes=15)
    mark = _mark("counterfactual-horizon-mark", Decimal("101"), mark_at)
    await observations.observe(mark)
    await observer.observe(mark, context_events=(mark,))
    await observer.observe(mark, context_events=(mark,))
    funding_at = entry_book.observed_at + timedelta(hours=8)
    funding_event = ObservedMarketEvent(
        venue="binance_usdm",
        stream="rest:/fapi/v1/fundingRate",
        symbol="BTCUSDT",
        event_id="binance_usdm:funding:BTCUSDT:counterfactual:Regular",
        kind=MarketEventKind.FUNDING_SETTLEMENT,
        venue_event_at=funding_at,
        observed_at=funding_at + timedelta(milliseconds=50),
        sequence=None,
        sequence_not_applicable_reason="binance_funding_history_has_no_sequence",
        payload=FundingSettlementPayload(
            funding_at=funding_at,
            funding_rate=Decimal("0.001"),
            mark_price=Decimal("101"),
            rate_type="Regular",
        ),
    )
    await observer.observe(funding_event, context_events=(funding_event,))
    await observer.observe(funding_event, context_events=(funding_event,))

    async with uow_factory.begin() as uow:
        unresolved = await uow.counterfactuals.get_unresolved(command.manifest.experiment_id)
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)
        funding_count = len(
            (
                await uow.session.scalars(
                    select(FundingEntryModel).where(
                        FundingEntryModel.experiment_id == command.manifest.experiment_id
                    )
                )
            ).all()
        )

    assert len(unresolved) == 1
    counterfactual = unresolved[0]
    assert counterfactual.status is CounterfactualStatus.OPEN
    assert counterfactual.outcome("15m") is not None
    assert counterfactual.funding == -(counterfactual.quantity * Decimal("0.101"))
    assert (
        sum(event.event_type == "counterfactual.mark_observed" for event in counterfactual.events)
        == 1
    )
    assert (
        sum(event.event_type == "counterfactual.funding_applied" for event in counterfactual.events)
        == 1
    )
    assert account.version == 0
    assert funding_count == 0


async def test_first_eligible_counterfactual_book_records_explicit_no_fill(
    uow_factory: UnitOfWork,
) -> None:
    command, outcome, observations, observer = await _runtime_with_pending_counterfactual(
        uow_factory
    )
    assert outcome.counterfactual is not None
    eligible_after = outcome.counterfactual.eligible_after
    mark = _mark("counterfactual-no-fill-mark", Decimal("100"), eligible_after)
    shallow = _event_book(
        "counterfactual-shallow-book",
        eligible_after + timedelta(milliseconds=1),
        "100",
        "101",
        5_000,
        quantity="0.00000001",
    )
    await observations.observe(mark)
    await observations.observe(shallow)

    await observer.observe(shallow, context_events=(mark, shallow))
    await observer.observe(shallow, context_events=(mark, shallow))

    async with uow_factory.begin() as uow:
        counterfactual = await uow.counterfactuals.get(outcome.counterfactual.counterfactual_id)
        account = await uow.paper_execution.load_account(command.manifest.experiment_id)

    assert counterfactual.status is CounterfactualStatus.NO_FILL
    assert counterfactual.no_fill_reason == "insufficient_visible_depth"
    assert counterfactual.entry_fill is None
    assert account.version == 0
