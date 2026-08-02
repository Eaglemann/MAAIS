from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.domain.enums import PaperOrderSide
from maais.execution.paper.fills import FillRejection, MarketFillEngine, MarketFillRequest
from maais.execution.paper.market import BookLevel, BookSnapshot

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _book(
    *,
    event_id: str = "book-1",
    observed_at: datetime = NOW + timedelta(milliseconds=101),
) -> BookSnapshot:
    return BookSnapshot(
        event_id=event_id,
        symbol="BTCUSDT",
        venue_event_at=observed_at - timedelta(milliseconds=10),
        observed_at=observed_at,
        sequence=10,
        bids=(BookLevel(Decimal("99.50"), Decimal("3")),),
        asks=(
            BookLevel(Decimal("101.00"), Decimal("1")),
            BookLevel(Decimal("102.00"), Decimal("2")),
        ),
        mark_price=Decimal("100.25"),
    )


def test_market_buy_walks_only_visible_ask_depth_with_exact_costs() -> None:
    engine = MarketFillEngine(max_book_age=timedelta(milliseconds=250))
    request = MarketFillRequest(
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("2"),
        eligible_after=NOW + timedelta(milliseconds=100),
        decision_executable_price=Decimal("100.80"),
        taker_fee_rate=Decimal("0.0005"),
    )

    fill = engine.fill(request, (_book(),))

    assert fill.market_event_id == "book-1"
    assert fill.quantity == Decimal("2")
    assert fill.price == Decimal("101.50")
    assert fill.notional == Decimal("203.00")
    assert fill.slices[0].quantity == Decimal("1")
    assert fill.slices[1].quantity == Decimal("1")
    assert fill.spread_cost == Decimal("1.50")
    assert fill.depth_slippage == Decimal("1.00")
    assert fill.latency_slippage == Decimal("0.40")
    assert fill.total_slippage == Decimal("2.90")
    assert fill.fee == Decimal("0.101500")


def test_market_sell_walks_bids_and_uses_sell_cost_signs() -> None:
    book = replace(
        _book(),
        bids=(
            BookLevel(Decimal("100"), Decimal("1")),
            BookLevel(Decimal("99"), Decimal("2")),
        ),
        asks=(BookLevel(Decimal("101"), Decimal("3")),),
        mark_price=Decimal("100.5"),
    )
    request = MarketFillRequest(
        symbol="BTCUSDT",
        side=PaperOrderSide.SELL,
        quantity=Decimal("2"),
        eligible_after=NOW + timedelta(milliseconds=100),
        decision_executable_price=Decimal("100.20"),
        taker_fee_rate=Decimal("0.001"),
    )

    fill = MarketFillEngine(timedelta(seconds=1)).fill(request, (book,))

    assert fill.price == Decimal("99.50")
    assert fill.spread_cost == Decimal("1.00")
    assert fill.depth_slippage == Decimal("1.00")
    assert fill.latency_slippage == Decimal("0.40")
    assert fill.fee == Decimal("0.19900")


def test_first_eligible_book_is_authoritative_even_if_later_book_has_depth() -> None:
    shallow = replace(
        _book(event_id="shallow"),
        asks=(BookLevel(Decimal("101"), Decimal("0.5")),),
    )
    deep_later = replace(
        _book(
            event_id="deep-later",
            observed_at=NOW + timedelta(milliseconds=102),
        ),
        asks=(BookLevel(Decimal("101"), Decimal("100")),),
    )
    request = MarketFillRequest(
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("1"),
        eligible_after=NOW + timedelta(milliseconds=100),
        decision_executable_price=Decimal("101"),
        taker_fee_rate=Decimal("0.0005"),
    )

    with pytest.raises(FillRejection, match="insufficient_visible_depth") as error:
        MarketFillEngine(timedelta(seconds=1)).fill(request, (deep_later, shallow))

    assert error.value.market_event_id == "shallow"


def test_market_fill_rejects_stale_or_missing_eligible_book() -> None:
    request = MarketFillRequest(
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("1"),
        eligible_after=NOW + timedelta(milliseconds=100),
        decision_executable_price=Decimal("101"),
        taker_fee_rate=Decimal("0.0005"),
    )
    stale = replace(
        _book(),
        venue_event_at=NOW - timedelta(seconds=2),
    )

    with pytest.raises(FillRejection, match="stale_book"):
        MarketFillEngine(timedelta(milliseconds=250)).fill(request, (stale,))
    with pytest.raises(FillRejection, match="no_eligible_book"):
        MarketFillEngine(timedelta(milliseconds=250)).fill(
            request,
            (replace(_book(), observed_at=request.eligible_after),),
        )


def test_book_rejects_crossed_or_malformed_depth() -> None:
    with pytest.raises(ValueError, match="strictly descending"):
        replace(
            _book(),
            bids=(BookLevel(Decimal("99"), Decimal("1")), BookLevel(Decimal("100"), Decimal("1"))),
        )
    with pytest.raises(ValueError, match="not be crossed"):
        replace(
            _book(),
            bids=(BookLevel(Decimal("102"), Decimal("1")),),
        )
