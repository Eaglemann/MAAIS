from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from maais.domain.json import JsonValue
from maais.execution.paper.account import AccountState
from maais.execution.paper.clock import require_utc
from maais.execution.paper.exits import ExitPlan, ExitPlanStatus
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import require_positive_decimal
from maais.execution.paper.orders import PaperOrder


@dataclass(frozen=True, slots=True)
class ExecutionFillRecord:
    id: UUID
    order_intent_id: UUID
    market_event_id: str
    fill_at: datetime
    quantity: Decimal
    price: Decimal
    liquidity_role: str
    fee: Decimal
    fee_asset: str
    spread_cost: Decimal
    depth_slippage: Decimal
    latency_slippage: Decimal
    total_slippage: Decimal
    market_snapshot: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.market_event_id or not self.liquidity_role or not self.fee_asset:
            raise ValueError("market event, liquidity role, and fee asset are required")
        require_utc(self.fill_at, "fill_at")
        require_positive_decimal(self.quantity, "fill quantity")
        require_positive_decimal(self.price, "fill price")
        for value, field in (
            (self.fee, "fee"),
            (self.spread_cost, "spread_cost"),
            (self.depth_slippage, "depth_slippage"),
            (self.latency_slippage, "latency_slippage"),
            (self.total_slippage, "total_slippage"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{field} must be a finite Decimal")
        if self.fee < 0:
            raise ValueError("fee must be nonnegative")
        if self.total_slippage != (self.spread_cost + self.depth_slippage + self.latency_slippage):
            raise ValueError("total slippage does not reconcile to its components")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "order_intent_id": self.order_intent_id,
            "market_event_id": self.market_event_id,
            "fill_at": self.fill_at,
            "quantity": self.quantity,
            "price": self.price,
            "liquidity_role": self.liquidity_role,
            "fee": self.fee,
            "fee_asset": self.fee_asset,
            "spread_cost": self.spread_cost,
            "depth_slippage": self.depth_slippage,
            "latency_slippage": self.latency_slippage,
            "total_slippage": self.total_slippage,
            "market_snapshot": self.market_snapshot,
        }


@dataclass(frozen=True, slots=True)
class PaperExecutionRecord:
    order: PaperOrder
    exchange_filters: ExchangeFilterSnapshot
    fills: tuple[ExecutionFillRecord, ...]
    account: AccountState | None
    exit_plan: ExitPlan | None

    def validate(self) -> None:
        if self.account is not None and self.order.experiment_id != self.account.experiment_id:
            raise ValueError("order and account experiment differ")
        if self.order.symbol != self.exchange_filters.symbol:
            raise ValueError("order and exchange filter symbol differ")
        if any(fill.order_intent_id != self.order.order_id for fill in self.fills):
            raise ValueError("fill order identity differs")
        total_filled = sum((fill.quantity for fill in self.fills), start=Decimal("0"))
        if total_filled != self.order.filled_quantity:
            raise ValueError("fill projection does not match order filled quantity")
        if self.fills and self.account is None:
            raise ValueError("filled execution requires account state")
        if self.account is None and self.exit_plan is not None:
            raise ValueError("exit plan requires account state")
        if self.account is not None and not self.account.reconcile().ok:
            raise ValueError("account does not reconcile")
        if self.exit_plan is not None:
            assert self.account is not None
            position = self.account.position(self.order.symbol)
            if self.exit_plan.position_id != position.position_id:
                raise ValueError("exit plan position identity differs")
            if not position.is_flat and self.exit_plan.quantity != position.quantity:
                raise ValueError("exit plan quantity differs from position")
            if position.is_flat and self.exit_plan.status is not ExitPlanStatus.CLOSED:
                raise ValueError("flat position requires a closed exit plan")


@dataclass(frozen=True, slots=True)
class FundingRecord:
    id: UUID
    experiment_id: UUID
    position_id: UUID
    market_event_id: str
    funding_at: datetime
    observed_at: datetime
    rate: Decimal
    rate_type: str
    mark_price: Decimal
    notional: Decimal
    amount: Decimal

    def __post_init__(self) -> None:
        if not self.market_event_id:
            raise ValueError("market_event_id is required")
        require_utc(self.funding_at, "funding_at")
        require_utc(self.observed_at, "observed_at")
        if self.observed_at < self.funding_at:
            raise ValueError("funding cannot be observed before its venue timestamp")
        if self.rate_type not in {"Regular", "Special"}:
            raise ValueError("funding rate_type must be Regular or Special")
        require_positive_decimal(self.mark_price, "funding mark price")
        require_positive_decimal(self.notional, "funding notional")
        for value, field in ((self.rate, "rate"), (self.amount, "amount")):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"funding {field} must be a finite Decimal")
