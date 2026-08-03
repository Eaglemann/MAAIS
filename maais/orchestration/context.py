"""Restart-safe assembly of official entry-decision context."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from maais.db.unit_of_work import UnitOfWork
from maais.execution.paper.clock import require_utc
from maais.execution.paper.exits import ExitPlanStatus
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import ReferenceKind, ReferencePricePayload
from maais.market_data.frames import CausalMinuteFrame
from maais.market_data.history import CausalFrameHistory
from maais.monitoring.admission import (
    BenchmarkObservation,
    MonitoringAdmissionContext,
    RollingVolatilityBaseline,
)
from maais.operations.controls import TradingControlSnapshot
from maais.orchestration.commands import EntryDecisionContext
from maais.orchestration.observations import MarketObservationBuffer, RuntimeHealthRegistry
from maais.risk.official import CorrelationObservation, DrawdownSnapshot, OpenRiskPosition


class RuntimeStateConflict(RuntimeError):
    pass


class TradingControlPort(Protocol):
    async def current(self, experiment_id: UUID) -> TradingControlSnapshot: ...


class PersistentTradingControls:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def current(self, experiment_id: UUID) -> TradingControlSnapshot:
        async with self._uow.begin() as transaction:
            return await transaction.controls.current(experiment_id)


class LiveEntryContextAssembler:
    def __init__(
        self,
        *,
        uow: UnitOfWork,
        manifest: ExperimentManifest,
        policy: LivePaperPolicy,
        history: CausalFrameHistory,
        observations: MarketObservationBuffer,
        health: RuntimeHealthRegistry,
        controls: TradingControlPort,
        exchange_filters: Mapping[str, ExchangeFilterSnapshot],
    ) -> None:
        filters = dict(exchange_filters)
        if set(filters) != set(manifest.symbols) or any(
            symbol != item.symbol for symbol, item in filters.items()
        ):
            raise ValueError("exchange filters must cover the exact manifest symbols")
        mismatched_hashes = sorted(
            symbol
            for symbol, item in filters.items()
            if item.content_hash != policy.exchange_filter_hashes[symbol]
        )
        if mismatched_hashes:
            raise ValueError(
                "exchange filters differ from manifest snapshots: " + ", ".join(mismatched_hashes)
            )
        self._uow = uow
        self._manifest = manifest
        self._policy = policy
        self._history = history
        self._observations = observations
        self._health = health
        self._controls = controls
        self._filters = filters

    async def build(
        self,
        frame: CausalMinuteFrame,
        *,
        evaluated_at: datetime,
        completed_at: datetime,
    ) -> EntryDecisionContext:
        require_utc(evaluated_at, "entry context evaluated_at")
        require_utc(completed_at, "entry context completed_at")
        if completed_at < evaluated_at:
            raise ValueError("entry context completion cannot precede evaluation")
        if (
            frame.key.experiment_id != self._manifest.experiment_id
            or frame.key.symbol not in self._manifest.symbols
        ):
            raise ValueError("entry context frame differs from manifest")
        async with self._uow.begin() as transaction:
            account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
            pending = await transaction.paper_execution.load_pending_orders(
                self._manifest.experiment_id
            )
            exit_plans = await transaction.paper_execution.load_open_exit_plans(
                self._manifest.experiment_id
            )
        if pending:
            raise RuntimeStateConflict(
                "pending paper orders require explicit startup reconciliation"
            )
        if account.leverage != self._policy.leverage:
            raise RuntimeStateConflict("restored account leverage differs from run policy")

        plans_by_position = {plan.position_id: plan for plan in exit_plans}
        if len(plans_by_position) != len(exit_plans):
            raise RuntimeStateConflict("open exit plans are duplicated by position")
        open_positions: list[OpenRiskPosition] = []
        for position in account.positions.values():
            if position.is_flat:
                continue
            plan = plans_by_position.get(position.position_id)
            if plan is None:
                raise RuntimeStateConflict("open paper position has no protective exit plan")
            loss_at_stop = position.quantity * abs(position.average_entry - plan.stop_price)
            if loss_at_stop <= 0:
                raise RuntimeStateConflict("open position has no positive loss at stop")
            open_positions.append(
                OpenRiskPosition(
                    symbol=position.symbol,
                    notional=position.gross_notional,
                    loss_at_stop=loss_at_stop,
                    margin=position.gross_notional / Decimal(account.leverage),
                )
            )
        orphan_plans = set(plans_by_position) - {
            position.position_id for position in account.positions.values() if not position.is_flat
        }
        if orphan_plans:
            raise RuntimeStateConflict("protective exit plan has no open paper position")

        control = await self._controls.current(self._manifest.experiment_id)
        if control.experiment_id != self._manifest.experiment_id:
            raise RuntimeStateConflict("trading control belongs to another experiment")
        triggered = tuple(plan for plan in exit_plans if plan.status is ExitPlanStatus.TRIGGERED)
        if triggered:
            raise RuntimeStateConflict(
                "triggered protective exits require explicit startup reconciliation"
            )

        books = await self._observations.books_after(
            frame.key.symbol,
            completed_at + self._policy.execution_latency,
            timeout=self._policy.book_wait_timeout,
        )
        active_exit_plan = None
        current_position = account.positions.get(frame.key.symbol)
        if current_position is not None and not current_position.is_flat:
            active_exit_plan = plans_by_position[current_position.position_id]
        monitoring = MonitoringAdmissionContext(
            symbol=frame.key.symbol,
            timeframe=frame.key.timeframe,
            evaluated_at=evaluated_at,
            kill_switch_active=control.kill_switch_active,
            kill_switch_reason=control.reason,
            kill_switch_version=control.version,
            kill_switch_changed_at=control.changed_at,
            kill_switch_changed_by=control.changed_by,
            health=self._health.snapshot(),
            volatility=self._volatility(frame),
            benchmark=self._benchmark(frame, evaluated_at=evaluated_at),
        )
        return EntryDecisionContext(
            monitoring=monitoring,
            drawdown=DrawdownSnapshot(account.peak_equity, account.equity),
            open_positions=tuple(open_positions),
            correlations=self._correlations(
                frame.key.symbol,
                tuple(item.symbol for item in open_positions),
            ),
            exchange_filters=self._filters[frame.key.symbol],
            account=account,
            books=books,
            active_exit_plan=active_exit_plan,
            proposal_ttl=self._policy.proposal_ttl,
            execution_latency=self._policy.execution_latency,
            taker_fee_rate=self._policy.taker_fee_rate,
        )

    def _volatility(self, frame: CausalMinuteFrame) -> RollingVolatilityBaseline | None:
        series = self._history.close_series(frame.key.symbol)
        prior_returns = _return_series(series)
        if len(prior_returns) < 2 or frame.best_bid is None or frame.best_ask is None:
            return None
        baseline_values = tuple(prior_returns.values())[-60:]
        baseline_std = _sample_std(baseline_values)
        current_values = (*tuple(prior_returns.values()), _close_return(series, frame.bar.close))
        current_std = _sample_std(current_values[-14:])
        if baseline_std is None or baseline_std <= 0 or current_std is None:
            return None
        midpoint = (frame.best_bid + frame.best_ask) / Decimal("2")
        source = frame.source_manifest.get("closed_bar")
        if source is None:
            return None
        return RollingVolatilityBaseline(
            symbol=frame.key.symbol,
            timeframe=frame.key.timeframe,
            sample_count=len(baseline_values),
            baseline_std=baseline_std,
            current_std=current_std,
            spread_fraction=(frame.best_ask - frame.best_bid) / midpoint,
            observed_at=source.observed_at,
        )

    def _benchmark(
        self,
        frame: CausalMinuteFrame,
        *,
        evaluated_at: datetime,
    ) -> BenchmarkObservation | None:
        base = self._history.benchmark_base(
            self._policy.benchmark_symbol,
            horizon_bars=self._policy.benchmark_horizon_bars,
        )
        if base is None or base.primary_spot_price is None:
            return None
        if frame.key.symbol == self._policy.benchmark_symbol:
            source = frame.source_manifest.get("primary_spot")
            current_price = frame.primary_spot_price
            if source is None or current_price is None:
                return None
            observed_at = source.observed_at
            source_event_id = source.event_id
        else:
            event = self._observations.latest_primary_reference(
                self._policy.benchmark_symbol,
                at_or_before=evaluated_at,
            )
            if event is None or not isinstance(event.payload, ReferencePricePayload):
                return None
            if event.payload.reference_kind is not ReferenceKind.PRIMARY_SPOT:
                return None
            current_price = event.payload.price
            observed_at = event.observed_at
            source_event_id = event.event_id
        return BenchmarkObservation(
            symbol=self._policy.benchmark_symbol,
            return_fraction=(current_price - base.primary_spot_price) / base.primary_spot_price,
            observed_at=observed_at,
            source_event_id=source_event_id,
        )

    def _correlations(
        self,
        symbol: str,
        open_symbols: tuple[str, ...],
    ) -> tuple[CorrelationObservation, ...]:
        left = _return_series(self._history.close_series(symbol))
        result: list[CorrelationObservation] = []
        for other in sorted(set(open_symbols) - {symbol}):
            right = _return_series(self._history.close_series(other))
            aligned_times = sorted(set(left) & set(right))[-60:]
            pairs = tuple((left[item], right[item]) for item in aligned_times)
            result.append(
                CorrelationObservation(
                    other_symbol=other,
                    aligned_return_count=len(pairs),
                    correlation=_correlation(pairs),
                )
            )
        return tuple(result)


def _return_series(
    series: tuple[tuple[datetime, Decimal], ...],
) -> dict[datetime, Decimal]:
    return {
        current_at: (current - previous) / previous
        for (_previous_at, previous), (current_at, current) in zip(series, series[1:])
    }


def _close_return(
    series: tuple[tuple[datetime, Decimal], ...],
    current: Decimal,
) -> Decimal:
    if not series:
        raise RuntimeStateConflict("current volatility has no previous close")
    previous = series[-1][1]
    return (current - previous) / previous


def _sample_std(values: tuple[Decimal, ...]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values, start=Decimal("0")) / Decimal(len(values))
    variance = sum(
        ((value - mean) ** 2 for value in values),
        start=Decimal("0"),
    ) / Decimal(len(values) - 1)
    return variance.sqrt()


def _correlation(pairs: tuple[tuple[Decimal, Decimal], ...]) -> Decimal | None:
    if len(pairs) < 2:
        return None
    left = tuple(item[0] for item in pairs)
    right = tuple(item[1] for item in pairs)
    left_mean = sum(left, start=Decimal("0")) / Decimal(len(left))
    right_mean = sum(right, start=Decimal("0")) / Decimal(len(right))
    covariance = sum(
        (
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in pairs
        ),
        start=Decimal("0"),
    )
    left_ss = sum(((value - left_mean) ** 2 for value in left), start=Decimal("0"))
    right_ss = sum(((value - right_mean) ** 2 for value in right), start=Decimal("0"))
    denominator = (left_ss * right_ss).sqrt()
    if denominator == 0:
        return None
    return covariance / denominator
