from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from maais.domain.enums import Direction, GateType, PaperOrderSide
from maais.domain.json import JsonValue, freeze_json
from maais.execution.paper.clock import require_utc
from maais.execution.paper.exits import ExitPlan
from maais.execution.paper.fills import PaperFill
from maais.execution.paper.market import require_positive_decimal

_HORIZONS = (
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("4h", timedelta(hours=4)),
    ("24h", timedelta(hours=24)),
)


class CounterfactualStatus(StrEnum):
    PENDING = "pending"
    NO_FILL = "no_fill"
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class HorizonOutcome:
    horizon: str
    observed_at: datetime
    mark_price: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class CounterfactualTransition:
    sequence: int
    event_type: str
    event_at: datetime
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class CounterfactualState:
    counterfactual_id: UUID
    experiment_id: UUID
    proposal_id: UUID
    decision_cycle_id: UUID
    symbol: str
    direction: Direction
    rejection_gate: GateType
    prior_gate_chain: tuple[GateType, ...]
    quantity: Decimal
    eligible_after: datetime
    fee_rate: Decimal
    expected_loss_fraction: Decimal
    expected_gain_fraction: Decimal
    status: CounterfactualStatus
    entry_fill: PaperFill | None
    exit_plan: ExitPlan | None
    maximum_favorable_excursion: Decimal
    maximum_adverse_excursion: Decimal
    outcomes: tuple[HorizonOutcome, ...]
    funding: Decimal
    no_fill_reason: str | None
    hypothetical_exit_reason: str | None
    hypothetical_pnl: Decimal | None
    created_at: datetime
    closed_at: datetime | None
    version: int
    events: tuple[CounterfactualTransition, ...]

    @classmethod
    def create(
        cls,
        *,
        counterfactual_id: UUID,
        experiment_id: UUID,
        proposal_id: UUID,
        decision_cycle_id: UUID,
        symbol: str,
        direction: Direction,
        rejection_gate: GateType,
        prior_gate_chain: tuple[GateType, ...],
        quantity: Decimal,
        eligible_after: datetime,
        fee_rate: Decimal,
        expected_loss_fraction: Decimal,
        expected_gain_fraction: Decimal,
        created_at: datetime,
    ) -> CounterfactualState:
        if direction not in {Direction.LONG, Direction.SHORT}:
            raise ValueError("counterfactual direction must be long or short")
        if not symbol or not prior_gate_chain or prior_gate_chain[-1] is not rejection_gate:
            raise ValueError("prior gate chain must end at the rejection gate")
        require_positive_decimal(quantity, "quantity")
        _require_rate(fee_rate, "fee_rate", allow_zero=True)
        _require_rate(expected_loss_fraction, "expected_loss_fraction")
        _require_rate(expected_gain_fraction, "expected_gain_fraction")
        require_utc(eligible_after, "eligible_after")
        require_utc(created_at, "created_at")
        payload = _payload(
            {
                "proposal_id": proposal_id,
                "decision_cycle_id": decision_cycle_id,
                "symbol": symbol,
                "direction": direction,
                "rejection_gate": rejection_gate,
                "prior_gate_chain": prior_gate_chain,
                "quantity": quantity,
                "eligible_after": eligible_after,
            }
        )
        event = CounterfactualTransition(1, "counterfactual.created", created_at, payload)
        return cls(
            counterfactual_id=counterfactual_id,
            experiment_id=experiment_id,
            proposal_id=proposal_id,
            decision_cycle_id=decision_cycle_id,
            symbol=symbol,
            direction=direction,
            rejection_gate=rejection_gate,
            prior_gate_chain=prior_gate_chain,
            quantity=quantity,
            eligible_after=eligible_after,
            fee_rate=fee_rate,
            expected_loss_fraction=expected_loss_fraction,
            expected_gain_fraction=expected_gain_fraction,
            status=CounterfactualStatus.PENDING,
            entry_fill=None,
            exit_plan=None,
            maximum_favorable_excursion=Decimal("0"),
            maximum_adverse_excursion=Decimal("0"),
            outcomes=(),
            funding=Decimal("0"),
            no_fill_reason=None,
            hypothetical_exit_reason=None,
            hypothetical_pnl=None,
            created_at=created_at,
            closed_at=None,
            version=1,
            events=(event,),
        )

    def enter(self, fill: PaperFill, *, plan_id: UUID) -> CounterfactualState:
        if self.status is not CounterfactualStatus.PENDING:
            raise RuntimeError("counterfactual is not pending")
        expected_side = (
            PaperOrderSide.BUY if self.direction is Direction.LONG else PaperOrderSide.SELL
        )
        if fill.symbol != self.symbol or fill.side is not expected_side:
            raise ValueError("hypothetical fill does not match counterfactual direction")
        if fill.quantity != self.quantity or fill.fill_at <= self.eligible_after:
            raise ValueError("hypothetical fill quantity or eligibility differs")
        plan = ExitPlan.create(
            plan_id=plan_id,
            position_id=self.counterfactual_id,
            side=self.direction,
            quantity=fill.quantity,
            average_entry=fill.price,
            expected_loss_fraction=self.expected_loss_fraction,
            expected_gain_fraction=self.expected_gain_fraction,
            created_at=fill.fill_at,
        )
        return self._advance(
            status=CounterfactualStatus.OPEN,
            event_type="counterfactual.entered",
            event_at=fill.fill_at,
            payload={
                "market_event_id": fill.market_event_id,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "total_slippage": fill.total_slippage,
            },
            entry_fill=fill,
            exit_plan=plan,
        )

    def mark_no_fill(self, reason: str, observed_at: datetime) -> CounterfactualState:
        if self.status is not CounterfactualStatus.PENDING:
            raise RuntimeError("counterfactual is not pending")
        if not reason:
            raise ValueError("no-fill reason is required")
        require_utc(observed_at, "observed_at")
        return self._advance(
            status=CounterfactualStatus.NO_FILL,
            event_type="counterfactual.no_fill",
            event_at=observed_at,
            payload={"reason": reason},
            no_fill_reason=reason,
            closed_at=observed_at,
        )

    def observe_mark(self, mark_price: Decimal, observed_at: datetime) -> CounterfactualState:
        self._require_open()
        require_positive_decimal(mark_price, "mark_price")
        require_utc(observed_at, "observed_at")
        assert self.entry_fill is not None
        assert self.exit_plan is not None
        gross = self._gross_pnl(mark_price)
        favorable = max(self.maximum_favorable_excursion, max(Decimal("0"), gross))
        adverse = max(self.maximum_adverse_excursion, max(Decimal("0"), -gross))
        outcomes = list(self.outcomes)
        resolved_horizons = {outcome.horizon for outcome in outcomes}
        for horizon, duration in _HORIZONS:
            if (
                horizon not in resolved_horizons
                and observed_at >= self.entry_fill.fill_at + duration
            ):
                outcomes.append(
                    HorizonOutcome(
                        horizon=horizon,
                        observed_at=observed_at,
                        mark_price=mark_price,
                        net_pnl=self._net_pnl(mark_price),
                    )
                )
        updated = self._advance(
            status=self.status,
            event_type="counterfactual.mark_observed",
            event_at=observed_at,
            payload={
                "mark_price": mark_price,
                "gross_pnl": gross,
                "maximum_favorable_excursion": favorable,
                "maximum_adverse_excursion": adverse,
                "resolved_horizons": [outcome.horizon for outcome in outcomes],
            },
            maximum_favorable_excursion=favorable,
            maximum_adverse_excursion=adverse,
            outcomes=tuple(outcomes),
        )
        assert updated.exit_plan is not None
        evaluation = updated.exit_plan.evaluate_mark(mark_price, observed_at)
        updated = replace(updated, exit_plan=evaluation.plan)
        if evaluation.intent is not None:
            return updated._close(mark_price, evaluation.intent.reason.value, observed_at)
        return updated

    def observe_closed_bar(
        self,
        *,
        mark_price: Decimal,
        decision_direction: Direction,
        decision_approved: bool,
        closed_at: datetime,
    ) -> CounterfactualState:
        updated = self.observe_mark(mark_price, closed_at)
        if updated.status is not CounterfactualStatus.OPEN:
            return updated
        assert updated.exit_plan is not None
        evaluation = updated.exit_plan.observe_closed_bar(
            decision_direction=decision_direction,
            decision_approved=decision_approved,
            closed_at=closed_at,
            executable_price=mark_price,
        )
        updated = replace(updated, exit_plan=evaluation.plan)
        if evaluation.intent is not None:
            return updated._close(mark_price, evaluation.intent.reason.value, closed_at)
        return updated

    def apply_funding(
        self, rate: Decimal, mark_price: Decimal, observed_at: datetime
    ) -> CounterfactualState:
        self._require_open()
        if not isinstance(rate, Decimal) or not rate.is_finite():
            raise ValueError("funding rate must be a finite Decimal")
        require_positive_decimal(mark_price, "mark_price")
        require_utc(observed_at, "observed_at")
        amount = self.quantity * mark_price * rate
        if self.direction is Direction.LONG:
            amount = -amount
        return self._advance(
            status=self.status,
            event_type="counterfactual.funding_applied",
            event_at=observed_at,
            payload={"rate": rate, "mark_price": mark_price, "amount": amount},
            funding=self.funding + amount,
        )

    def outcome(self, horizon: str) -> Decimal | None:
        return next(
            (outcome.net_pnl for outcome in self.outcomes if outcome.horizon == horizon),
            None,
        )

    def _close(
        self,
        mark_price: Decimal,
        reason: str,
        closed_at: datetime,
    ) -> CounterfactualState:
        pnl = self._net_pnl(mark_price)
        return self._advance(
            status=CounterfactualStatus.RESOLVED,
            event_type="counterfactual.resolved",
            event_at=closed_at,
            payload={"mark_price": mark_price, "reason": reason, "net_pnl": pnl},
            hypothetical_exit_reason=reason,
            hypothetical_pnl=pnl,
            closed_at=closed_at,
        )

    def _gross_pnl(self, mark_price: Decimal) -> Decimal:
        assert self.entry_fill is not None
        difference = mark_price - self.entry_fill.price
        if self.direction is Direction.SHORT:
            difference = -difference
        return difference * self.quantity

    def _net_pnl(self, mark_price: Decimal) -> Decimal:
        assert self.entry_fill is not None
        exit_fee = mark_price * self.quantity * self.fee_rate
        return self._gross_pnl(mark_price) - self.entry_fill.fee - exit_fee + self.funding

    def _require_open(self) -> None:
        if self.status is not CounterfactualStatus.OPEN:
            raise RuntimeError("counterfactual is not open")

    def _advance(
        self,
        *,
        status: CounterfactualStatus,
        event_type: str,
        event_at: datetime,
        payload: object,
        **changes: object,
    ) -> CounterfactualState:
        require_utc(event_at, "event_at")
        event = CounterfactualTransition(
            sequence=self.version + 1,
            event_type=event_type,
            event_at=event_at,
            payload=_payload(payload),
        )
        return replace(
            self,
            status=status,
            version=self.version + 1,
            events=(*self.events, event),
            **changes,
        )


def _payload(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("counterfactual event payload must be an object")
    return normalized


def _require_rate(value: Decimal, field: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    lower_ok = value >= 0 if allow_zero else value > 0
    if not lower_ok or value >= 1:
        interval = "[0, 1)" if allow_zero else "(0, 1)"
        raise ValueError(f"{field} must be a finite Decimal in {interval}")
