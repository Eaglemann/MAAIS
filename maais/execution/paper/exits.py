from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from maais.domain.enums import Direction, PaperOrderSide, PaperOrderType
from maais.execution.paper.clock import require_utc
from maais.execution.paper.market import require_positive_decimal


class ExitPlanStatus(StrEnum):
    ACTIVE = "active"
    TRIGGERED = "triggered"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    MAXIMUM_HOLD = "maximum_hold"
    OPPOSING_SIGNAL = "opposing_signal"
    EMERGENCY = "emergency"


@dataclass(frozen=True, slots=True)
class ExitIntent:
    position_id: UUID
    side: PaperOrderSide
    order_type: PaperOrderType
    quantity: Decimal
    reduce_only: bool
    reason: ExitReason
    triggered_at: datetime
    trigger_price: Decimal | None


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    plan: ExitPlan
    intent: ExitIntent | None


@dataclass(frozen=True, slots=True)
class ExitPlan:
    plan_id: UUID
    position_id: UUID
    side: Direction
    quantity: Decimal
    average_entry: Decimal
    expected_loss_fraction: Decimal
    expected_gain_fraction: Decimal
    stop_price: Decimal
    target_price: Decimal
    maximum_bars: int
    bars_elapsed: int
    opposite_signal_streak: int
    status: ExitPlanStatus
    created_at: datetime
    changed_at: datetime
    version: int
    trigger_reason: ExitReason | None
    triggered_at: datetime | None
    trigger_price: Decimal | None
    trigger_executable_price: Decimal | None

    def __post_init__(self) -> None:
        trigger_fields = (
            self.trigger_reason,
            self.triggered_at,
            self.trigger_executable_price,
        )
        if self.status is ExitPlanStatus.ACTIVE and any(
            value is not None for value in (*trigger_fields, self.trigger_price)
        ):
            raise ValueError("active exit plan cannot contain trigger metadata")
        if self.status in {ExitPlanStatus.TRIGGERED, ExitPlanStatus.CLOSED} and any(
            value is None for value in trigger_fields
        ):
            raise ValueError("triggered and closed exit plans require restart metadata")
        if self.triggered_at is not None:
            require_utc(self.triggered_at, "triggered_at")
            if self.triggered_at < self.created_at:
                raise ValueError("exit trigger cannot precede plan creation")
        if self.trigger_price is not None:
            require_positive_decimal(self.trigger_price, "trigger_price")
        if self.trigger_executable_price is not None:
            require_positive_decimal(
                self.trigger_executable_price,
                "trigger_executable_price",
            )

    @classmethod
    def create(
        cls,
        *,
        plan_id: UUID,
        position_id: UUID,
        side: Direction,
        quantity: Decimal,
        average_entry: Decimal,
        expected_loss_fraction: Decimal,
        expected_gain_fraction: Decimal,
        created_at: datetime,
        maximum_bars: int = 60,
    ) -> ExitPlan:
        if side not in {Direction.LONG, Direction.SHORT}:
            raise ValueError("exit plan side must be long or short")
        require_positive_decimal(quantity, "quantity")
        require_positive_decimal(average_entry, "average_entry")
        _require_fraction(expected_loss_fraction, "expected_loss_fraction")
        _require_fraction(expected_gain_fraction, "expected_gain_fraction")
        require_utc(created_at, "created_at")
        if maximum_bars <= 0:
            raise ValueError("maximum_bars must be positive")
        stop, target = _levels(
            side,
            average_entry,
            expected_loss_fraction,
            expected_gain_fraction,
        )
        return cls(
            plan_id=plan_id,
            position_id=position_id,
            side=side,
            quantity=quantity,
            average_entry=average_entry,
            expected_loss_fraction=expected_loss_fraction,
            expected_gain_fraction=expected_gain_fraction,
            stop_price=stop,
            target_price=target,
            maximum_bars=maximum_bars,
            bars_elapsed=0,
            opposite_signal_streak=0,
            status=ExitPlanStatus.ACTIVE,
            created_at=created_at,
            changed_at=created_at,
            version=1,
            trigger_reason=None,
            triggered_at=None,
            trigger_price=None,
            trigger_executable_price=None,
        )

    def evaluate_mark(
        self,
        mark_price: Decimal,
        observed_at: datetime,
        *,
        executable_price: Decimal | None = None,
    ) -> ExitEvaluation:
        self._require_active()
        require_positive_decimal(mark_price, "mark_price")
        require_utc(observed_at, "observed_at")
        if executable_price is not None:
            require_positive_decimal(executable_price, "executable_price")
        if self.side is Direction.LONG:
            if mark_price <= self.stop_price:
                return self._trigger(
                    ExitReason.STOP,
                    observed_at,
                    mark_price,
                    executable_price or mark_price,
                )
            if mark_price >= self.target_price:
                return self._trigger(
                    ExitReason.TARGET,
                    observed_at,
                    mark_price,
                    executable_price or mark_price,
                )
        else:
            if mark_price >= self.stop_price:
                return self._trigger(
                    ExitReason.STOP,
                    observed_at,
                    mark_price,
                    executable_price or mark_price,
                )
            if mark_price <= self.target_price:
                return self._trigger(
                    ExitReason.TARGET,
                    observed_at,
                    mark_price,
                    executable_price or mark_price,
                )
        return ExitEvaluation(self, None)

    def observe_closed_bar(
        self,
        *,
        decision_direction: Direction,
        decision_approved: bool,
        closed_at: datetime,
        executable_price: Decimal,
    ) -> ExitEvaluation:
        self._require_active()
        require_utc(closed_at, "closed_at")
        require_positive_decimal(executable_price, "executable_price")
        opposing = decision_approved and (
            (self.side is Direction.LONG and decision_direction is Direction.SHORT)
            or (self.side is Direction.SHORT and decision_direction is Direction.LONG)
        )
        streak = self.opposite_signal_streak + 1 if opposing else 0
        updated = replace(
            self,
            bars_elapsed=self.bars_elapsed + 1,
            opposite_signal_streak=streak,
            changed_at=closed_at,
            version=self.version + 1,
        )
        if streak >= 2:
            return updated._trigger(
                ExitReason.OPPOSING_SIGNAL,
                closed_at,
                None,
                executable_price,
            )
        if updated.bars_elapsed >= updated.maximum_bars:
            return updated._trigger(
                ExitReason.MAXIMUM_HOLD,
                closed_at,
                None,
                executable_price,
            )
        return ExitEvaluation(updated, None)

    def resize(
        self,
        *,
        quantity: Decimal,
        average_entry: Decimal,
        changed_at: datetime,
    ) -> ExitPlan:
        self._require_active()
        require_positive_decimal(quantity, "quantity")
        require_positive_decimal(average_entry, "average_entry")
        require_utc(changed_at, "changed_at")
        proposed_stop, target = _levels(
            self.side,
            average_entry,
            self.expected_loss_fraction,
            self.expected_gain_fraction,
        )
        stop = (
            max(self.stop_price, proposed_stop)
            if self.side is Direction.LONG
            else min(self.stop_price, proposed_stop)
        )
        return replace(
            self,
            quantity=quantity,
            average_entry=average_entry,
            stop_price=stop,
            target_price=target,
            changed_at=changed_at,
            version=self.version + 1,
        )

    def tighten_stop(self, stop_price: Decimal, changed_at: datetime) -> ExitPlan:
        self._require_active()
        require_positive_decimal(stop_price, "stop_price")
        require_utc(changed_at, "changed_at")
        moves_away = (self.side is Direction.LONG and stop_price < self.stop_price) or (
            self.side is Direction.SHORT and stop_price > self.stop_price
        )
        if moves_away:
            raise ValueError("stop cannot move away from risk")
        return replace(
            self,
            stop_price=stop_price,
            changed_at=changed_at,
            version=self.version + 1,
        )

    def emergency_flatten(
        self,
        triggered_at: datetime,
        *,
        executable_price: Decimal,
    ) -> ExitEvaluation:
        self._require_active()
        require_utc(triggered_at, "triggered_at")
        require_positive_decimal(executable_price, "executable_price")
        return self._trigger(
            ExitReason.EMERGENCY,
            triggered_at,
            None,
            executable_price,
        )

    def pending_intent(self) -> ExitIntent:
        if self.status is not ExitPlanStatus.TRIGGERED:
            raise RuntimeError("only a triggered exit plan has a pending intent")
        assert self.trigger_reason is not None
        assert self.triggered_at is not None
        side = PaperOrderSide.SELL if self.side is Direction.LONG else PaperOrderSide.BUY
        return ExitIntent(
            position_id=self.position_id,
            side=side,
            order_type=PaperOrderType.MARKET,
            quantity=self.quantity,
            reduce_only=True,
            reason=self.trigger_reason,
            triggered_at=self.triggered_at,
            trigger_price=self.trigger_price,
        )

    def close(self, closed_at: datetime) -> ExitPlan:
        require_utc(closed_at, "closed_at")
        if self.status is not ExitPlanStatus.TRIGGERED:
            raise RuntimeError("only a triggered exit plan can be closed")
        return replace(
            self,
            status=ExitPlanStatus.CLOSED,
            changed_at=closed_at,
            version=self.version + 1,
        )

    def _trigger(
        self,
        reason: ExitReason,
        triggered_at: datetime,
        trigger_price: Decimal | None,
        trigger_executable_price: Decimal,
    ) -> ExitEvaluation:
        side = PaperOrderSide.SELL if self.side is Direction.LONG else PaperOrderSide.BUY
        triggered = replace(
            self,
            status=ExitPlanStatus.TRIGGERED,
            changed_at=triggered_at,
            version=self.version + 1,
            trigger_reason=reason,
            triggered_at=triggered_at,
            trigger_price=trigger_price,
            trigger_executable_price=trigger_executable_price,
        )
        return ExitEvaluation(
            triggered,
            ExitIntent(
                position_id=self.position_id,
                side=side,
                order_type=PaperOrderType.MARKET,
                quantity=self.quantity,
                reduce_only=True,
                reason=reason,
                triggered_at=triggered_at,
                trigger_price=trigger_price,
            ),
        )

    def _require_active(self) -> None:
        if self.status is not ExitPlanStatus.ACTIVE:
            raise RuntimeError(f"exit plan is not active: {self.status.value}")


def _require_fraction(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or not Decimal("0") < value < 1:
        raise ValueError(f"{field} must be a finite Decimal in (0, 1)")


def _levels(
    side: Direction,
    average_entry: Decimal,
    expected_loss_fraction: Decimal,
    expected_gain_fraction: Decimal,
) -> tuple[Decimal, Decimal]:
    if side is Direction.LONG:
        return (
            average_entry * (Decimal("1") - expected_loss_fraction),
            average_entry * (Decimal("1") + expected_gain_fraction),
        )
    return (
        average_entry * (Decimal("1") + expected_loss_fraction),
        average_entry * (Decimal("1") - expected_gain_fraction),
    )
