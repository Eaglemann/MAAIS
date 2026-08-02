from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import Direction, PaperOrderSide
from maais.execution.paper.exits import ExitPlan, ExitReason

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("side", "stop", "target", "stop_side"),
    (
        (Direction.LONG, Decimal("99"), Decimal("101"), PaperOrderSide.SELL),
        (Direction.SHORT, Decimal("101"), Decimal("99"), PaperOrderSide.BUY),
    ),
)
def test_exit_plan_uses_actual_fill_and_triggers_boundaries(
    side: Direction,
    stop: Decimal,
    target: Decimal,
    stop_side: PaperOrderSide,
) -> None:
    plan = ExitPlan.create(
        plan_id=UUID(int=1),
        position_id=UUID(int=2),
        side=side,
        quantity=Decimal("2"),
        average_entry=Decimal("100"),
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
        maximum_bars=60,
    )

    assert plan.stop_price == stop
    assert plan.target_price == target
    result = plan.evaluate_mark(stop, NOW + timedelta(seconds=1))
    assert result.intent is not None
    assert result.intent.reason is ExitReason.STOP
    assert result.intent.side is stop_side
    assert result.intent.quantity == Decimal("2")
    assert result.intent.reduce_only


def test_maximum_hold_and_opposite_signal_are_closed_bar_based() -> None:
    plan = ExitPlan.create(
        plan_id=UUID(int=1),
        position_id=UUID(int=2),
        side=Direction.LONG,
        quantity=Decimal("1"),
        average_entry=Decimal("100"),
        expected_loss_fraction=Decimal("0.1"),
        expected_gain_fraction=Decimal("0.1"),
        created_at=NOW,
        maximum_bars=60,
    )
    for bar in range(1, 60):
        result = plan.observe_closed_bar(
            decision_direction=Direction.NEUTRAL,
            decision_approved=False,
            closed_at=NOW + timedelta(minutes=bar),
        )
        assert result.intent is None
        plan = result.plan
    result = plan.observe_closed_bar(
        decision_direction=Direction.NEUTRAL,
        decision_approved=False,
        closed_at=NOW + timedelta(minutes=60),
    )
    assert result.intent is not None
    assert result.intent.reason is ExitReason.MAXIMUM_HOLD

    plan = ExitPlan.create(
        plan_id=UUID(int=3),
        position_id=UUID(int=4),
        side=Direction.LONG,
        quantity=Decimal("1"),
        average_entry=Decimal("100"),
        expected_loss_fraction=Decimal("0.1"),
        expected_gain_fraction=Decimal("0.1"),
        created_at=NOW,
        maximum_bars=60,
    )
    first = plan.observe_closed_bar(
        decision_direction=Direction.SHORT,
        decision_approved=True,
        closed_at=NOW + timedelta(minutes=1),
    )
    reset = first.plan.observe_closed_bar(
        decision_direction=Direction.NEUTRAL,
        decision_approved=False,
        closed_at=NOW + timedelta(minutes=2),
    )
    first_again = reset.plan.observe_closed_bar(
        decision_direction=Direction.SHORT,
        decision_approved=True,
        closed_at=NOW + timedelta(minutes=3),
    )
    second = first_again.plan.observe_closed_bar(
        decision_direction=Direction.SHORT,
        decision_approved=True,
        closed_at=NOW + timedelta(minutes=4),
    )
    assert first.intent is None
    assert reset.plan.opposite_signal_streak == 0
    assert second.intent is not None
    assert second.intent.reason is ExitReason.OPPOSING_SIGNAL


def test_partial_resize_never_moves_stop_away_from_risk() -> None:
    long_plan = ExitPlan.create(
        plan_id=UUID(int=1),
        position_id=UUID(int=2),
        side=Direction.LONG,
        quantity=Decimal("1"),
        average_entry=Decimal("100"),
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
        maximum_bars=60,
    )
    resized = long_plan.resize(
        quantity=Decimal("2"),
        average_entry=Decimal("90"),
        changed_at=NOW + timedelta(seconds=1),
    )
    tightened = resized.tighten_stop(Decimal("99.5"), NOW + timedelta(seconds=2))

    assert resized.stop_price == Decimal("99")
    assert resized.quantity == Decimal("2")
    assert tightened.stop_price == Decimal("99.5")
    with pytest.raises(ValueError, match="away from risk"):
        tightened.tighten_stop(Decimal("98"), NOW + timedelta(seconds=3))


def test_emergency_flatten_is_exact_and_reduce_only() -> None:
    plan = ExitPlan.create(
        plan_id=UUID(int=1),
        position_id=UUID(int=2),
        side=Direction.SHORT,
        quantity=Decimal("0.75"),
        average_entry=Decimal("100"),
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
        maximum_bars=60,
    )

    result = plan.emergency_flatten(NOW + timedelta(seconds=1))

    assert result.intent is not None
    assert result.intent.reason is ExitReason.EMERGENCY
    assert result.intent.side is PaperOrderSide.BUY
    assert result.intent.quantity == Decimal("0.75")
    assert result.intent.reduce_only
