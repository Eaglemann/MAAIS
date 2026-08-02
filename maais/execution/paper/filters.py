from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from maais.domain.enums import PaperOrderSide, PaperOrderType
from maais.domain.json import content_hash
from maais.execution.paper.clock import require_utc


def _require_positive(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a positive finite Decimal")


def _multiple(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    units = (value / increment).to_integral_value(rounding=rounding)
    return units * increment


class FilterRejection(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class PreparedOrder:
    symbol: str
    side: PaperOrderSide
    order_type: PaperOrderType
    requested_quantity: Decimal
    approved_quantity: Decimal
    quantity: Decimal
    price: Decimal | None
    notional: Decimal


@dataclass(frozen=True, slots=True)
class ExchangeFilterSnapshot:
    symbol: str
    status: str
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    minimum_notional: Decimal
    supported_order_types: tuple[PaperOrderType, ...]
    captured_at: datetime

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        for field in (
            "price_tick",
            "quantity_step",
            "minimum_quantity",
            "maximum_quantity",
            "minimum_notional",
        ):
            _require_positive(getattr(self, field), field)
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("minimum_quantity cannot exceed maximum_quantity")
        if not self.supported_order_types:
            raise ValueError("supported_order_types cannot be empty")
        require_utc(self.captured_at, "captured_at")

    def to_dict(self) -> dict[str, object]:
        return {**self.rules_dict(), "captured_at": self.captured_at}

    def rules_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "price_tick": self.price_tick,
            "quantity_step": self.quantity_step,
            "minimum_quantity": self.minimum_quantity,
            "maximum_quantity": self.maximum_quantity,
            "minimum_notional": self.minimum_notional,
            "supported_order_types": self.supported_order_types,
        }

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    @property
    def rules_hash(self) -> str:
        return content_hash(self.rules_dict())

    def quantize_quantity(self, value: Decimal) -> Decimal:
        _require_positive(value, "requested_quantity")
        return _multiple(value, self.quantity_step, ROUND_FLOOR)

    def quantize_price(self, side: PaperOrderSide, value: Decimal) -> Decimal:
        _require_positive(value, "price")
        rounding = ROUND_FLOOR if side is PaperOrderSide.BUY else ROUND_CEILING
        return _multiple(value, self.price_tick, rounding)

    def prepare(
        self,
        *,
        side: PaperOrderSide,
        order_type: PaperOrderType,
        requested_quantity: Decimal,
        approved_quantity: Decimal,
        price: Decimal | None,
        approved_notional: Decimal,
    ) -> PreparedOrder:
        if self.status != "TRADING":
            raise FilterRejection("symbol_not_trading")
        if order_type not in self.supported_order_types:
            raise FilterRejection("unsupported_order_type")
        _require_positive(approved_quantity, "approved_quantity")
        _require_positive(approved_notional, "approved_notional")
        if requested_quantity > approved_quantity:
            raise FilterRejection("approved_quantity_exceeded")
        quantity = self.quantize_quantity(requested_quantity)
        if quantity < self.minimum_quantity:
            raise FilterRejection("quantity_below_minimum")
        if quantity > self.maximum_quantity:
            raise FilterRejection("quantity_above_maximum")
        quantized_price: Decimal | None = None
        if order_type is PaperOrderType.LIMIT:
            if price is None:
                raise FilterRejection("limit_price_required")
            quantized_price = self.quantize_price(side, price)
            notional = quantity * quantized_price
        else:
            if price is None:
                raise FilterRejection("validation_price_required")
            _require_positive(price, "price")
            notional = quantity * price
        if notional < self.minimum_notional:
            raise FilterRejection("notional_below_minimum")
        if notional > approved_notional:
            raise FilterRejection("approved_notional_exceeded")
        return PreparedOrder(
            symbol=self.symbol,
            side=side,
            order_type=order_type,
            requested_quantity=requested_quantity,
            approved_quantity=approved_quantity,
            quantity=quantity,
            price=quantized_price,
            notional=notional,
        )
