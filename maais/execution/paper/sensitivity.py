from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from maais.domain.enums import PaperOrderSide
from maais.execution.paper.clock import require_utc
from maais.execution.paper.fills import PaperFill
from maais.execution.paper.market import require_positive_decimal


class SensitivityScenario(StrEnum):
    OPTIMISTIC = "optimistic"
    CONSERVATIVE = "conservative"
    STRESS = "stress"


@dataclass(frozen=True, slots=True)
class SensitivityOutcome:
    scenario: SensitivityScenario
    calculated_at: datetime
    effective_fill_price: Decimal
    fee: Decimal
    execution_cost: Decimal
    marked_pnl: Decimal

    def __post_init__(self) -> None:
        require_utc(self.calculated_at, "calculated_at")
        require_positive_decimal(self.effective_fill_price, "effective_fill_price")
        for value, field in (
            (self.fee, "fee"),
            (self.execution_cost, "execution_cost"),
            (self.marked_pnl, "marked_pnl"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field} must be a finite Decimal")
        if self.fee < 0 or self.execution_cost < 0:
            raise ValueError("fee and execution_cost must be nonnegative")

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "calculated_at": self.calculated_at,
            "effective_fill_price": self.effective_fill_price,
            "fee": self.fee,
            "execution_cost": self.execution_cost,
            "marked_pnl": self.marked_pnl,
        }


def calculate_sensitivities(
    fill: PaperFill,
    *,
    marked_price: Decimal,
    calculated_at: datetime,
    stress_multiplier: Decimal = Decimal("2"),
) -> tuple[SensitivityOutcome, ...]:
    require_positive_decimal(marked_price, "marked_price")
    require_utc(calculated_at, "calculated_at")
    if (
        not isinstance(stress_multiplier, Decimal)
        or not stress_multiplier.is_finite()
        or stress_multiplier < 1
    ):
        raise ValueError("stress_multiplier must be a finite Decimal at least 1")
    best_price = (
        min(item.price for item in fill.slices)
        if fill.side is PaperOrderSide.BUY
        else max(item.price for item in fill.slices)
    )
    additional_stress = (
        abs(fill.total_slippage) / fill.quantity * (stress_multiplier - Decimal("1"))
    )
    stress_price = (
        fill.price + additional_stress
        if fill.side is PaperOrderSide.BUY
        else fill.price - additional_stress
    )
    require_positive_decimal(stress_price, "stress effective fill price")
    fee_rate = fill.fee / fill.notional if fill.notional > 0 else Decimal("0")

    def outcome(scenario: SensitivityScenario, price: Decimal) -> SensitivityOutcome:
        fee = price * fill.quantity * fee_rate
        slippage = abs(price - best_price) * fill.quantity
        gross = (marked_price - price) * fill.quantity
        if fill.side is PaperOrderSide.SELL:
            gross = -gross
        return SensitivityOutcome(
            scenario=scenario,
            calculated_at=calculated_at,
            effective_fill_price=price,
            fee=fee,
            execution_cost=fee + slippage,
            marked_pnl=gross - fee,
        )

    return (
        outcome(SensitivityScenario.OPTIMISTIC, best_price),
        outcome(SensitivityScenario.CONSERVATIVE, fill.price),
        outcome(SensitivityScenario.STRESS, stress_price),
    )
