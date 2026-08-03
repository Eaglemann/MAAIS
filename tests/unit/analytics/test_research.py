from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from maais.analytics.research import (
    AnalyticsBar,
    AnalyticsCounterfactual,
    AnalyticsFill,
    AnalyticsSensitivity,
    AnalyticsSnapshot,
    build_research_analytics,
)

NOW = datetime(2026, 8, 3, 8, tzinfo=timezone.utc)


def _fill(
    *,
    fill_id: int,
    effect: str,
    side: str,
    price: str,
    fee: str,
    at: datetime,
) -> AnalyticsFill:
    return AnalyticsFill(
        id=UUID(int=fill_id),
        symbol="BTCUSDT",
        side=side,
        position_effect=effect,
        quantity=Decimal("1"),
        price=Decimal(price),
        fee=Decimal(fee),
        fill_at=at,
        direction="long",
        decision_cycle_id=UUID(int=10),
        strategy_version_id=UUID(int=11),
        regime="trending",
        risk_at_stop=Decimal("5"),
        approved_quantity=Decimal("1"),
        consensus_probability=Decimal("0.8"),
        coalition=("momentum", "trend"),
        agent_probabilities={"momentum": Decimal("0.9")},
    )


def test_research_analytics_reconciles_performance_calibration_and_benchmarks() -> None:
    result = build_research_analytics(
        initial_capital=Decimal("1000"),
        snapshots=(
            AnalyticsSnapshot(
                snapshot_at=NOW,
                equity=Decimal("999"),
                drawdown=Decimal("0.001"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                fees=Decimal("1"),
                funding=Decimal("0"),
            ),
            AnalyticsSnapshot(
                snapshot_at=NOW + timedelta(hours=1),
                equity=Decimal("1008"),
                drawdown=Decimal("0"),
                realized_pnl=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                fees=Decimal("2"),
                funding=Decimal("0"),
            ),
        ),
        fills=(
            _fill(fill_id=1, effect="open", side="buy", price="100", fee="1", at=NOW),
            _fill(
                fill_id=2,
                effect="reduce",
                side="sell",
                price="110",
                fee="1",
                at=NOW + timedelta(hours=1),
            ),
        ),
        bars=(
            AnalyticsBar("BTCUSDT", NOW, Decimal("102"), Decimal("98"), Decimal("100")),
            AnalyticsBar(
                "BTCUSDT",
                NOW + timedelta(minutes=30),
                Decimal("112"),
                Decimal("97"),
                Decimal("108"),
            ),
            AnalyticsBar(
                "BTCUSDT",
                NOW + timedelta(hours=1),
                Decimal("111"),
                Decimal("109"),
                Decimal("110"),
            ),
        ),
        counterfactuals=(
            AnalyticsCounterfactual(
                rejection_gate="expected_value",
                status="resolved",
                hypothetical_pnl=Decimal("-4"),
                consensus_probability=Decimal("0.7"),
                agent_probabilities={"momentum": Decimal("0.6")},
            ),
        ),
        sensitivities=(
            AnalyticsSensitivity("optimistic", Decimal("2"), Decimal("11")),
            AnalyticsSensitivity("stress", Decimal("5"), Decimal("7")),
        ),
    )

    assert result["cost_waterfall"] == {
        "initial_capital": Decimal("1000"),
        "gross_realized_pnl": Decimal("10"),
        "fees": Decimal("-2"),
        "funding": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "net_change": Decimal("8"),
        "ending_equity": Decimal("1008"),
        "reconciles": True,
    }
    performance = result["performance"]
    assert performance["closed_trade_allocations"] == 1
    assert performance["win_rate"] == Decimal("1")
    assert performance["expectancy"] == Decimal("8")
    assert performance["profit_factor"] is None
    assert performance["average_r_multiple"] == Decimal("1.6")
    assert performance["maximum_favorable_excursion"] == Decimal("12")
    assert performance["maximum_adverse_excursion"] == Decimal("3")
    assert result["attribution"]["by_symbol"][0]["net_pnl_ex_funding"] == Decimal("8")
    assert result["calibration"]["consensus"]["sample_size"] == 2
    assert result["calibration"]["consensus"]["brier_score"] == Decimal("0.265")
    assert result["gate_value"]["by_gate"][0]["avoided_pnl"] == Decimal("4")
    assert result["cost_sensitivity"]["stress"]["marked_pnl"] == Decimal("7")
    assert result["benchmarks"]["buy_and_hold"]["ending_equity"] == Decimal("1100")
    assert result["benchmarks"]["flat_cash"]["ending_equity"] == Decimal("1000")


def test_research_analytics_labels_metrics_that_have_no_observations() -> None:
    result = build_research_analytics(
        initial_capital=Decimal("1000"),
        snapshots=(),
        fills=(),
        bars=(),
        counterfactuals=(),
        sensitivities=(),
    )

    assert result["equity_curve"] == []
    assert result["performance"]["win_rate"] is None
    assert result["benchmarks"]["buy_and_hold"]["status"] == "unavailable"
    assert result["availability"]["closed_trade_metrics"] == {
        "status": "unavailable",
        "reason": "no_closed_official_trade_allocations",
        "sample_size": 0,
    }
