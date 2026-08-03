import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import Direction, GateType, PaperOrderSide
from maais.execution.paper.fills import MarketFillEngine, MarketFillRequest
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.research import counterfactuals as module
from maais.research.counterfactuals import CounterfactualState, CounterfactualStatus

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _fill():
    observed = NOW + timedelta(milliseconds=101)
    book = BookSnapshot(
        event_id="book-1",
        symbol="BTCUSDT",
        venue_event_at=observed - timedelta(milliseconds=1),
        observed_at=observed,
        sequence=1,
        bids=(BookLevel(Decimal("100"), Decimal("2")),),
        asks=(BookLevel(Decimal("101.5"), Decimal("2")),),
        mark_price=Decimal("100.75"),
    )
    request = MarketFillRequest(
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("1"),
        eligible_after=NOW + timedelta(milliseconds=100),
        decision_executable_price=Decimal("101"),
        taker_fee_rate=Decimal("0.0005"),
    )
    return MarketFillEngine(timedelta(seconds=1)).fill(request, (book,))


def _state() -> CounterfactualState:
    return CounterfactualState.create(
        counterfactual_id=UUID(int=1),
        experiment_id=UUID(int=2),
        proposal_id=UUID(int=3),
        decision_cycle_id=UUID(int=4),
        symbol="BTCUSDT",
        direction=Direction.LONG,
        rejection_gate=GateType.EV,
        prior_gate_chain=(GateType.DATA_QUALITY, GateType.CONSENSUS, GateType.EV),
        quantity=Decimal("1"),
        decision_executable_price=Decimal("101"),
        eligible_after=NOW + timedelta(milliseconds=100),
        fee_rate=Decimal("0.0005"),
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
    )


def test_counterfactual_tracks_horizons_excursions_costs_and_standard_exit() -> None:
    fill = _fill()
    state = _state().enter(fill, plan_id=UUID(int=5))

    assert state.decision_executable_price == Decimal("101")
    state = state.observe_mark(
        Decimal("102"),
        fill.fill_at + timedelta(minutes=15),
        market_event_id="mark-15m",
    )

    assert state.status is CounterfactualStatus.OPEN
    assert state.maximum_favorable_excursion == Decimal("0.5")
    assert state.maximum_adverse_excursion == 0
    assert state.outcome("15m") == Decimal("0.39825")

    state = state.observe_mark(
        Decimal("98"),
        fill.fill_at + timedelta(minutes=16),
        market_event_id="mark-stop",
    )

    assert state.status is CounterfactualStatus.RESOLVED
    assert state.maximum_adverse_excursion == Decimal("3.5")
    assert state.hypothetical_exit_reason == "stop"
    assert state.hypothetical_pnl == Decimal("-3.59975")
    assert state.closed_at == fill.fill_at + timedelta(minutes=16)


def test_counterfactual_horizon_uses_first_observation_at_or_after_cutoff() -> None:
    fill = _fill()
    state = _state().enter(fill, plan_id=UUID(int=5))
    before = state.observe_mark(
        Decimal("101.6"),
        fill.fill_at + timedelta(minutes=14, seconds=59),
        market_event_id="mark-before-15m",
    )
    due = before.observe_mark(
        Decimal("101.7"),
        fill.fill_at + timedelta(minutes=15),
        market_event_id="mark-at-15m",
    )
    later = due.observe_mark(
        Decimal("101.8"),
        fill.fill_at + timedelta(minutes=20),
        market_event_id="mark-after-15m",
    )

    assert before.outcome("15m") is None
    assert due.outcome("15m") == later.outcome("15m")


def test_no_fill_is_terminal_and_counterfactual_module_has_no_account_dependency() -> None:
    state = _state().mark_no_fill("insufficient_visible_depth", NOW + timedelta(seconds=1))

    assert state.status is CounterfactualStatus.NO_FILL
    assert state.hypothetical_pnl is None
    source = inspect.getsource(module)
    assert "paper.account" not in source
    assert "AccountState" not in source


def test_counterfactual_rejects_float_funding_rate() -> None:
    state = _state().enter(_fill(), plan_id=UUID(int=5))

    with pytest.raises(ValueError, match="Decimal"):
        state.apply_funding(
            0.001,  # type: ignore[arg-type]
            Decimal("101"),
            NOW + timedelta(hours=8),
            market_event_id="funding-invalid-rate",
        )


def test_counterfactual_funding_reobservation_ignores_local_time_but_not_payload() -> None:
    state = _state().enter(_fill(), plan_id=UUID(int=5))
    observed_at = NOW + timedelta(hours=8)
    funded = state.apply_funding(
        Decimal("0.001"),
        Decimal("101"),
        observed_at,
        market_event_id="funding-reobserved",
    )

    assert (
        funded.apply_funding(
            Decimal("0.001"),
            Decimal("101"),
            observed_at + timedelta(minutes=1),
            market_event_id="funding-reobserved",
        )
        == funded
    )
    with pytest.raises(ValueError, match="different content"):
        funded.apply_funding(
            Decimal("0.002"),
            Decimal("101"),
            observed_at + timedelta(minutes=1),
            market_event_id="funding-reobserved",
        )


def test_closed_bars_advance_exit_policy_once_and_resolve_opposing_signal() -> None:
    state = _state().enter(_fill(), plan_id=UUID(int=5))
    assert state.entry_fill is not None
    first_at = state.entry_fill.fill_at + timedelta(minutes=1)

    first = state.observe_closed_bar(
        mark_price=Decimal("101"),
        decision_direction=Direction.SHORT,
        decision_approved=True,
        closed_at=first_at,
        market_event_id="closed-bar-1",
    )
    duplicate = first.observe_closed_bar(
        mark_price=Decimal("101"),
        decision_direction=Direction.SHORT,
        decision_approved=True,
        closed_at=first_at,
        market_event_id="closed-bar-1",
    )

    assert duplicate == first
    assert first.exit_plan is not None
    assert first.exit_plan.bars_elapsed == 1
    assert first.exit_plan.opposite_signal_streak == 1
    assert first.version == state.version + 2
    assert first.events[-1].event_type == "counterfactual.closed_bar_observed"

    second = first.observe_closed_bar(
        mark_price=Decimal("101"),
        decision_direction=Direction.SHORT,
        decision_approved=True,
        closed_at=first_at + timedelta(minutes=1),
        market_event_id="closed-bar-2",
    )

    assert second.status is CounterfactualStatus.RESOLVED
    assert second.hypothetical_exit_reason == "opposing_signal"
