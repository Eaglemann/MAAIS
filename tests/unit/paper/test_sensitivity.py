from datetime import datetime, timedelta, timezone
from decimal import Decimal

from maais.domain.enums import PaperOrderSide
from maais.execution.paper.fills import MarketFillEngine, MarketFillRequest
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.execution.paper.sensitivity import (
    SensitivityScenario,
    calculate_sensitivities,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def test_execution_sensitivity_is_ordered_and_official_fill_is_conservative() -> None:
    observed_at = NOW + timedelta(milliseconds=101)
    book = BookSnapshot(
        event_id="book",
        symbol="BTCUSDT",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=1,
        bids=(BookLevel(Decimal("99"), Decimal("3")),),
        asks=(
            BookLevel(Decimal("101"), Decimal("1")),
            BookLevel(Decimal("102"), Decimal("2")),
        ),
        mark_price=Decimal("100"),
    )
    fill = MarketFillEngine(timedelta(seconds=1)).fill(
        MarketFillRequest(
            symbol="BTCUSDT",
            side=PaperOrderSide.BUY,
            quantity=Decimal("2"),
            eligible_after=NOW + timedelta(milliseconds=100),
            decision_executable_price=Decimal("100.5"),
            taker_fee_rate=Decimal("0.0005"),
        ),
        (book,),
    )

    optimistic, conservative, stress = calculate_sensitivities(
        fill,
        marked_price=Decimal("105"),
        calculated_at=NOW + timedelta(minutes=15),
    )

    assert optimistic.scenario is SensitivityScenario.OPTIMISTIC
    assert conservative.scenario is SensitivityScenario.CONSERVATIVE
    assert stress.scenario is SensitivityScenario.STRESS
    assert optimistic.effective_fill_price < conservative.effective_fill_price
    assert conservative.effective_fill_price < stress.effective_fill_price
    assert optimistic.marked_pnl > conservative.marked_pnl > stress.marked_pnl
    assert conservative.effective_fill_price == fill.price
    assert conservative.fee == fill.fee
