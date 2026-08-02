from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import PaperOrderSide, PaperOrderStatus, PaperOrderType, PositionEffect
from maais.execution.paper.market import BookLevel, BookSnapshot, TradePrint
from maais.execution.paper.orders import (
    IllegalOrderTransition,
    LimitQueueState,
    PaperOrder,
    advance_limit_queue,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _book() -> BookSnapshot:
    return BookSnapshot(
        event_id="book",
        symbol="BTCUSDT",
        venue_event_at=NOW,
        observed_at=NOW + timedelta(milliseconds=10),
        sequence=1,
        bids=(BookLevel(Decimal("100"), Decimal("5")),),
        asks=(BookLevel(Decimal("101"), Decimal("5")),),
        mark_price=Decimal("100.5"),
    )


def _trade(event_id: str, milliseconds: int, price: str, quantity: str) -> TradePrint:
    observed_at = NOW + timedelta(milliseconds=milliseconds)
    return TradePrint(
        event_id=event_id,
        symbol="BTCUSDT",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=milliseconds,
        price=Decimal(price),
        quantity=Decimal(quantity),
        aggressor_side="sell",
    )


def test_limit_touch_does_not_fill_and_queue_ahead_is_consumed_first() -> None:
    state = LimitQueueState.from_book(
        order_id=UUID(int=1),
        side=PaperOrderSide.BUY,
        limit_price=Decimal("100"),
        quantity=Decimal("2"),
        eligible_after=NOW + timedelta(milliseconds=10),
        expires_at=NOW + timedelta(seconds=10),
        maker_fee_rate=Decimal("0.0002"),
        book=_book(),
    )

    touched = advance_limit_queue(state, _trade("touch", 11, "100", "100"))
    queued = advance_limit_queue(touched.state, _trade("queue", 12, "99.9", "4"))
    partial = advance_limit_queue(queued.state, _trade("partial", 13, "99.8", "3"))

    assert not touched.fills
    assert queued.state.queue_ahead == Decimal("1")
    assert not queued.fills
    assert partial.state.queue_ahead == 0
    assert partial.state.filled_quantity == Decimal("0.2")
    assert partial.fills[0].quantity == Decimal("0.2")
    assert partial.fills[0].price == Decimal("99.8")
    assert partial.fills[0].fee == Decimal("0.003992")


def test_limit_participation_is_capped_per_event_and_never_overfills() -> None:
    state = replace(
        LimitQueueState.from_book(
            order_id=UUID(int=1),
            side=PaperOrderSide.BUY,
            limit_price=Decimal("100"),
            quantity=Decimal("1"),
            eligible_after=NOW + timedelta(milliseconds=10),
            expires_at=NOW + timedelta(seconds=10),
            maker_fee_rate=Decimal("0.0002"),
            book=_book(),
        ),
        queue_ahead=Decimal("0"),
    )

    first = advance_limit_queue(state, _trade("one", 11, "99", "3"))
    second = advance_limit_queue(first.state, _trade("two", 12, "98", "100"))

    assert first.state.filled_quantity == Decimal("0.3")
    assert second.state.filled_quantity == Decimal("1.0")
    assert second.state.status is PaperOrderStatus.FILLED
    assert second.fills[0].quantity == Decimal("0.7")


def test_limit_expiry_is_terminal_and_ignores_late_volume() -> None:
    state = LimitQueueState.from_book(
        order_id=UUID(int=1),
        side=PaperOrderSide.BUY,
        limit_price=Decimal("100"),
        quantity=Decimal("1"),
        eligible_after=NOW + timedelta(milliseconds=10),
        expires_at=NOW + timedelta(milliseconds=12),
        maker_fee_rate=Decimal("0.0002"),
        book=_book(),
    )

    result = advance_limit_queue(state, _trade("late", 12, "90", "100"))

    assert result.state.status is PaperOrderStatus.EXPIRED
    assert not result.fills


def test_order_aggregate_transitions_and_reduce_only_bounds() -> None:
    order = PaperOrder.create(
        order_id=UUID(int=1),
        experiment_id=UUID(int=2),
        proposal_id=UUID(int=3),
        client_order_id="paper-1",
        command_hash="a" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.SELL,
        order_type=PaperOrderType.MARKET,
        position_effect=PositionEffect.REDUCE,
        quantity=Decimal("2"),
        limit_price=None,
        reduce_only=True,
        open_quantity=Decimal("2"),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    order = order.authorize(NOW + timedelta(milliseconds=1)).accept(NOW + timedelta(milliseconds=2))
    partial = order.apply_fill(Decimal("0.5"), NOW + timedelta(milliseconds=3))
    filled = partial.apply_fill(Decimal("1.5"), NOW + timedelta(milliseconds=4))

    assert [event.sequence for event in filled.events] == [1, 2, 3, 4, 5]
    assert partial.status is PaperOrderStatus.PARTIALLY_FILLED
    assert filled.status is PaperOrderStatus.FILLED
    assert filled.remaining_quantity == 0
    with pytest.raises(IllegalOrderTransition, match="terminal"):
        filled.cancel(NOW + timedelta(milliseconds=5))

    with pytest.raises(ValueError, match="open quantity"):
        PaperOrder.create(
            order_id=UUID(int=10),
            experiment_id=UUID(int=2),
            proposal_id=UUID(int=3),
            client_order_id="bad-reduce",
            command_hash="b" * 64,
            symbol="BTCUSDT",
            side=PaperOrderSide.SELL,
            order_type=PaperOrderType.MARKET,
            position_effect=PositionEffect.REDUCE,
            quantity=Decimal("2.1"),
            limit_price=None,
            reduce_only=True,
            open_quantity=Decimal("2"),
            created_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
        )
