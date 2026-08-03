from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

ZERO = Decimal("0")
ONE = Decimal("1")
BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    snapshot_at: datetime
    equity: Decimal
    drawdown: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    funding: Decimal


@dataclass(frozen=True, slots=True)
class AnalyticsBar:
    symbol: str
    bar_close_at: datetime
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class AnalyticsFill:
    id: UUID
    symbol: str
    side: str
    position_effect: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fill_at: datetime
    direction: str
    decision_cycle_id: UUID
    strategy_version_id: UUID
    regime: str
    risk_at_stop: Decimal | None
    approved_quantity: Decimal | None
    consensus_probability: Decimal | None
    coalition: tuple[str, ...]
    agent_probabilities: Mapping[str, Decimal]
    exit_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AnalyticsCounterfactual:
    rejection_gate: str
    status: str
    hypothetical_pnl: Decimal | None
    consensus_probability: Decimal | None
    agent_probabilities: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class AnalyticsSensitivity:
    scenario: str
    execution_cost: Decimal
    marked_pnl: Decimal


@dataclass(slots=True)
class _OpenLot:
    fill: AnalyticsFill
    remaining_quantity: Decimal
    remaining_fee: Decimal


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, start=ZERO) / Decimal(len(values))


def _metric_availability(sample_size: int, reason: str) -> dict[str, object]:
    return {
        "status": "available" if sample_size else "unavailable",
        "reason": None if sample_size else reason,
        "sample_size": sample_size,
    }


def _excursions(
    bars: Sequence[AnalyticsBar],
    *,
    symbol: str,
    opened_at: datetime,
    closed_at: datetime,
    direction: str,
    entry_price: Decimal,
    quantity: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    relevant = [
        bar for bar in bars if bar.symbol == symbol and opened_at <= bar.bar_close_at <= closed_at
    ]
    if not relevant:
        return None, None
    if direction == "long":
        favorable = (max(bar.high for bar in relevant) - entry_price) * quantity
        adverse = (entry_price - min(bar.low for bar in relevant)) * quantity
    else:
        favorable = (entry_price - min(bar.low for bar in relevant)) * quantity
        adverse = (max(bar.high for bar in relevant) - entry_price) * quantity
    return max(ZERO, favorable), max(ZERO, adverse)


def _closed_allocations(
    fills: Sequence[AnalyticsFill],
    bars: Sequence[AnalyticsBar],
) -> list[dict[str, object]]:
    lots: dict[str, deque[_OpenLot]] = defaultdict(deque)
    allocations: list[dict[str, object]] = []
    for fill in sorted(fills, key=lambda item: (item.fill_at, item.id)):
        if fill.position_effect == "open":
            lots[fill.symbol].append(_OpenLot(fill, fill.quantity, fill.fee))
            continue
        remaining = fill.quantity
        while remaining > ZERO and lots[fill.symbol]:
            lot = lots[fill.symbol][0]
            closing = min(remaining, lot.remaining_quantity)
            if lot.fill.direction == "long":
                gross_pnl = (fill.price - lot.fill.price) * closing
            else:
                gross_pnl = (lot.fill.price - fill.price) * closing
            opening_fee = lot.remaining_fee * closing / lot.remaining_quantity
            closing_fee = fill.fee * closing / fill.quantity
            net_pnl = gross_pnl - opening_fee - closing_fee
            risk = None
            if (
                lot.fill.risk_at_stop is not None
                and lot.fill.approved_quantity is not None
                and lot.fill.approved_quantity > ZERO
            ):
                risk = lot.fill.risk_at_stop * closing / lot.fill.approved_quantity
            mfe, mae = _excursions(
                bars,
                symbol=fill.symbol,
                opened_at=lot.fill.fill_at,
                closed_at=fill.fill_at,
                direction=lot.fill.direction,
                entry_price=lot.fill.price,
                quantity=closing,
            )
            allocations.append(
                {
                    "symbol": fill.symbol,
                    "direction": lot.fill.direction,
                    "strategy_version_id": str(lot.fill.strategy_version_id),
                    "regime": lot.fill.regime,
                    "agent_coalition": ", ".join(lot.fill.coalition) or "none",
                    "entry_hour_berlin": lot.fill.fill_at.astimezone(BERLIN).strftime("%H:00"),
                    "exit_reason": fill.exit_reason or "unclassified",
                    "gross_pnl": gross_pnl,
                    "fees": opening_fee + closing_fee,
                    "net_pnl_ex_funding": net_pnl,
                    "r_multiple": net_pnl / risk if risk is not None and risk > ZERO else None,
                    "mfe": mfe,
                    "mae": mae,
                    "consensus_probability": lot.fill.consensus_probability,
                    "agent_probabilities": lot.fill.agent_probabilities,
                }
            )
            lot.remaining_quantity -= closing
            lot.remaining_fee -= opening_fee
            remaining -= closing
            if lot.remaining_quantity == ZERO:
                lots[fill.symbol].popleft()
    return allocations


def _attribution(
    allocations: Sequence[dict[str, object]],
    field: str,
) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for allocation in allocations:
        groups[str(allocation[field])].append(allocation)
    rows: list[dict[str, object]] = []
    for key, items in sorted(groups.items()):
        pnls = [Decimal(str(item["net_pnl_ex_funding"])) for item in items]
        wins = sum(value > ZERO for value in pnls)
        rows.append(
            {
                "key": key,
                "trades": len(items),
                "wins": wins,
                "losses": sum(value < ZERO for value in pnls),
                "win_rate": Decimal(wins) / Decimal(len(items)),
                "net_pnl_ex_funding": sum(pnls, start=ZERO),
                "expectancy": _mean(pnls),
            }
        )
    return rows


def _calibration_rows(
    allocations: Sequence[dict[str, object]],
    counterfactuals: Sequence[AnalyticsCounterfactual],
) -> dict[str, object]:
    predictions: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)

    def add(
        outcome: Decimal,
        consensus: Decimal | None,
        agents: Mapping[str, Decimal],
    ) -> None:
        label = ONE if outcome > ZERO else ZERO
        if consensus is not None:
            predictions["consensus"].append((consensus, label))
        for name, probability in agents.items():
            predictions[name].append((probability, label))

    for allocation in allocations:
        add(
            Decimal(str(allocation["gross_pnl"])),
            allocation["consensus_probability"],  # type: ignore[arg-type]
            allocation["agent_probabilities"],  # type: ignore[arg-type]
        )
    for item in counterfactuals:
        if item.status == "resolved" and item.hypothetical_pnl is not None:
            add(item.hypothetical_pnl, item.consensus_probability, item.agent_probabilities)

    result: dict[str, object] = {}
    for name, values in sorted(predictions.items()):
        score = _mean([(probability - outcome) ** 2 for probability, outcome in values])
        result[name] = {
            "sample_size": len(values),
            "brier_score": score,
            "mean_probability": _mean([probability for probability, _ in values]),
            "observed_win_rate": _mean([outcome for _, outcome in values]),
        }
    if "consensus" not in result:
        result["consensus"] = {
            "sample_size": 0,
            "brier_score": None,
            "mean_probability": None,
            "observed_win_rate": None,
        }
    return result


def _benchmarks(
    initial_capital: Decimal,
    bars: Sequence[AnalyticsBar],
) -> dict[str, object]:
    by_symbol: dict[str, list[AnalyticsBar]] = defaultdict(list)
    for bar in sorted(bars, key=lambda item: (item.symbol, item.bar_close_at)):
        by_symbol[bar.symbol].append(bar)
    returns = {
        symbol: items[-1].close / items[0].close - ONE
        for symbol, items in by_symbol.items()
        if len(items) >= 2 and items[0].close > ZERO
    }
    if returns:
        equal_weight_return = sum(returns.values(), start=ZERO) / Decimal(len(returns))
        buy_and_hold: dict[str, object] = {
            "status": "available",
            "method": "equal_weight_long_first_to_last_observed_close_no_costs",
            "symbols": len(returns),
            "return": equal_weight_return,
            "ending_equity": initial_capital * (ONE + equal_weight_return),
            "returns_by_symbol": returns,
        }
    else:
        buy_and_hold = {
            "status": "unavailable",
            "reason": "fewer_than_two_market_frames_per_symbol",
            "symbols": 0,
            "return": None,
            "ending_equity": None,
            "returns_by_symbol": {},
        }
    return {
        "buy_and_hold": buy_and_hold,
        "flat_cash": {
            "status": "available",
            "method": "initial_capital_held_in_cash_zero_interest",
            "return": ZERO,
            "ending_equity": initial_capital,
        },
    }


def _cost_sensitivity(rows: Sequence[AnalyticsSensitivity]) -> dict[str, object]:
    grouped: dict[str, list[AnalyticsSensitivity]] = defaultdict(list)
    for row in rows:
        grouped[row.scenario].append(row)
    return {
        scenario: {
            "sample_size": len(items),
            "execution_cost": sum((item.execution_cost for item in items), start=ZERO),
            "marked_pnl": sum((item.marked_pnl for item in items), start=ZERO),
        }
        for scenario, items in sorted(grouped.items())
    }


def build_research_analytics(
    *,
    initial_capital: Decimal,
    snapshots: Iterable[AnalyticsSnapshot],
    fills: Iterable[AnalyticsFill],
    bars: Iterable[AnalyticsBar],
    counterfactuals: Iterable[AnalyticsCounterfactual],
    sensitivities: Iterable[AnalyticsSensitivity],
) -> dict[str, object]:
    """Build deterministic read-only analytics from authoritative ledger projections."""
    snapshot_rows = sorted(snapshots, key=lambda item: item.snapshot_at)
    fill_rows = tuple(fills)
    bar_rows = tuple(bars)
    counterfactual_rows = tuple(counterfactuals)
    sensitivity_rows = tuple(sensitivities)
    allocations = _closed_allocations(fill_rows, bar_rows)
    latest = snapshot_rows[-1] if snapshot_rows else None
    ending_equity = latest.equity if latest is not None else initial_capital
    gross_realized = latest.realized_pnl if latest is not None else ZERO
    fees = latest.fees if latest is not None else ZERO
    funding = latest.funding if latest is not None else ZERO
    unrealized = latest.unrealized_pnl if latest is not None else ZERO
    expected_ending = initial_capital + gross_realized - fees + funding + unrealized
    pnls = [Decimal(str(item["net_pnl_ex_funding"])) for item in allocations]
    wins = [value for value in pnls if value > ZERO]
    losses = [value for value in pnls if value < ZERO]
    r_multiples = [
        Decimal(str(item["r_multiple"])) for item in allocations if item["r_multiple"] is not None
    ]
    mfes = [Decimal(str(item["mfe"])) for item in allocations if item["mfe"] is not None]
    maes = [Decimal(str(item["mae"])) for item in allocations if item["mae"] is not None]
    resolved_counterfactuals = [
        item
        for item in counterfactual_rows
        if item.status == "resolved" and item.hypothetical_pnl is not None
    ]
    by_gate: dict[str, list[Decimal]] = defaultdict(list)
    for item in resolved_counterfactuals:
        assert item.hypothetical_pnl is not None
        by_gate[item.rejection_gate].append(item.hypothetical_pnl)

    return {
        "equity_curve": [
            {
                "at": row.snapshot_at,
                "equity": row.equity,
                "drawdown": row.drawdown,
            }
            for row in snapshot_rows
        ],
        "cost_waterfall": {
            "initial_capital": initial_capital,
            "gross_realized_pnl": gross_realized,
            "fees": -fees,
            "funding": funding,
            "unrealized_pnl": unrealized,
            "net_change": ending_equity - initial_capital,
            "ending_equity": ending_equity,
            "reconciles": ending_equity == expected_ending,
        },
        "performance": {
            "basis": "fifo_closed_fill_allocations_net_of_open_and_close_fees_ex_funding",
            "closed_trade_allocations": len(allocations),
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(pnls) - len(wins) - len(losses),
            "win_rate": Decimal(len(wins)) / Decimal(len(pnls)) if pnls else None,
            "average_win": _mean(wins),
            "average_loss": _mean(losses),
            "expectancy": _mean(pnls),
            "profit_factor": (
                sum(wins, start=ZERO) / abs(sum(losses, start=ZERO)) if losses else None
            ),
            "pnl_distribution": pnls,
            "r_multiples": r_multiples,
            "average_r_multiple": _mean(r_multiples),
            "mfe_distribution": mfes,
            "mae_distribution": maes,
            "average_mfe": _mean(mfes),
            "average_mae": _mean(maes),
            "maximum_favorable_excursion": max(mfes) if mfes else None,
            "maximum_adverse_excursion": max(maes) if maes else None,
        },
        "attribution": {
            "by_symbol": _attribution(allocations, "symbol"),
            "by_regime": _attribution(allocations, "regime"),
            "by_strategy": _attribution(allocations, "strategy_version_id"),
            "by_agent_coalition": _attribution(allocations, "agent_coalition"),
            "by_hour_berlin": _attribution(allocations, "entry_hour_berlin"),
            "by_direction": _attribution(allocations, "direction"),
            "by_exit_reason": _attribution(allocations, "exit_reason"),
        },
        "calibration": _calibration_rows(allocations, counterfactual_rows),
        "gate_value": {
            "interpretation": "positive_avoided_pnl_means_the_rejection_avoided_a_loss",
            "resolved_sample_size": len(resolved_counterfactuals),
            "by_gate": [
                {
                    "gate": gate,
                    "sample_size": len(values),
                    "hypothetical_pnl": sum(values, start=ZERO),
                    "avoided_pnl": -sum(values, start=ZERO),
                }
                for gate, values in sorted(by_gate.items())
            ],
        },
        "cost_sensitivity": _cost_sensitivity(sensitivity_rows),
        "benchmarks": _benchmarks(initial_capital, bar_rows),
        "availability": {
            "closed_trade_metrics": _metric_availability(
                len(allocations), "no_closed_official_trade_allocations"
            ),
            "mfe_mae": _metric_availability(len(mfes), "no_closed_trade_market_path"),
            "r_multiples": _metric_availability(len(r_multiples), "risk_at_stop_unavailable"),
            "calibration": _metric_availability(
                len(allocations) + len(resolved_counterfactuals),
                "no_resolved_outcomes",
            ),
            "gate_value": _metric_availability(
                len(resolved_counterfactuals), "no_resolved_counterfactuals"
            ),
            "funding_attribution": {
                "status": "unavailable",
                "reason": (
                    "funding_is_authoritative_at_account_level_but_not_allocated_to_close_fills"
                ),
                "sample_size": 0,
            },
        },
    }
