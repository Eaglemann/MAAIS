from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from maais.domain.enums import Direction
from maais.execution.paper.clock import require_utc
from maais.execution.paper.market import require_positive_decimal


@dataclass(frozen=True, slots=True)
class PositionLot:
    lot_id: UUID
    opening_fill_id: UUID
    opened_at: datetime
    entry_price: Decimal
    original_quantity: Decimal
    remaining_quantity: Decimal
    opening_fee: Decimal
    remaining_opening_fee: Decimal
    funding: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class PositionState:
    position_id: UUID
    experiment_id: UUID
    symbol: str
    side: Direction
    mark_price: Decimal
    average_entry: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    lots: tuple[PositionLot, ...]
    opened_at: datetime | None
    closed_at: datetime | None
    version: int

    @classmethod
    def empty(cls, position_id: UUID, experiment_id: UUID, symbol: str) -> PositionState:
        if not symbol:
            raise ValueError("symbol is required")
        return cls(
            position_id=position_id,
            experiment_id=experiment_id,
            symbol=symbol,
            side=Direction.NEUTRAL,
            mark_price=Decimal("0"),
            average_entry=Decimal("0"),
            realized_pnl=Decimal("0"),
            fees=Decimal("0"),
            funding=Decimal("0"),
            lots=(),
            opened_at=None,
            closed_at=None,
            version=0,
        )

    @property
    def quantity(self) -> Decimal:
        return sum((lot.remaining_quantity for lot in self.lots), start=Decimal("0"))

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.is_flat:
            return Decimal("0")
        if self.side is Direction.LONG:
            return sum(
                ((self.mark_price - lot.entry_price) * lot.remaining_quantity for lot in self.lots),
                start=Decimal("0"),
            )
        return sum(
            ((lot.entry_price - self.mark_price) * lot.remaining_quantity for lot in self.lots),
            start=Decimal("0"),
        )

    @property
    def gross_notional(self) -> Decimal:
        return self.quantity * self.mark_price

    def open_fill(
        self,
        *,
        fill_id: UUID,
        side: Direction,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fill_at: datetime,
    ) -> PositionState:
        if side not in {Direction.LONG, Direction.SHORT}:
            raise ValueError("opening side must be long or short")
        if not self.is_flat and self.side is not side:
            raise ValueError("one-way position can only scale in the same direction")
        require_positive_decimal(quantity, "quantity")
        require_positive_decimal(price, "price")
        _require_nonnegative(fee, "fee")
        require_utc(fill_at, "fill_at")
        lot = PositionLot(
            lot_id=fill_id,
            opening_fill_id=fill_id,
            opened_at=fill_at,
            entry_price=price,
            original_quantity=quantity,
            remaining_quantity=quantity,
            opening_fee=fee,
            remaining_opening_fee=fee,
        )
        lots = (*self.lots, lot)
        total_quantity = self.quantity + quantity
        average_entry = (
            sum(
                (item.entry_price * item.remaining_quantity for item in lots),
                start=Decimal("0"),
            )
            / total_quantity
        )
        return replace(
            self,
            side=side,
            mark_price=price,
            average_entry=average_entry,
            fees=self.fees + fee,
            lots=lots,
            opened_at=self.opened_at if not self.is_flat else fill_at,
            closed_at=None,
            version=self.version + 1,
        )

    def close_fill(
        self,
        *,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fill_at: datetime,
    ) -> PositionState:
        require_positive_decimal(quantity, "quantity")
        require_positive_decimal(price, "price")
        _require_nonnegative(fee, "fee")
        require_utc(fill_at, "fill_at")
        if self.is_flat or quantity > self.quantity:
            raise ValueError("close quantity exceeds open quantity")

        remaining_to_close = quantity
        realized = Decimal("0")
        updated: list[PositionLot] = []
        for lot in self.lots:
            if remaining_to_close <= 0 or lot.remaining_quantity <= 0:
                updated.append(lot)
                continue
            closing = min(lot.remaining_quantity, remaining_to_close)
            if self.side is Direction.LONG:
                realized += (price - lot.entry_price) * closing
            else:
                realized += (lot.entry_price - price) * closing
            fee_allocation = lot.remaining_opening_fee * closing / lot.remaining_quantity
            updated.append(
                replace(
                    lot,
                    remaining_quantity=lot.remaining_quantity - closing,
                    remaining_opening_fee=lot.remaining_opening_fee - fee_allocation,
                )
            )
            remaining_to_close -= closing

        remaining_quantity = self.quantity - quantity
        if remaining_quantity > 0:
            average_entry = (
                sum(
                    (lot.entry_price * lot.remaining_quantity for lot in updated),
                    start=Decimal("0"),
                )
                / remaining_quantity
            )
            side = self.side
            closed_at = None
        else:
            average_entry = Decimal("0")
            side = Direction.NEUTRAL
            closed_at = fill_at
        return replace(
            self,
            side=side,
            mark_price=price,
            average_entry=average_entry,
            realized_pnl=self.realized_pnl + realized,
            fees=self.fees + fee,
            lots=tuple(updated),
            closed_at=closed_at,
            version=self.version + 1,
        )

    def mark(self, price: Decimal) -> PositionState:
        require_positive_decimal(price, "mark price")
        return replace(self, mark_price=price, version=self.version + 1)

    def apply_funding(self, amount: Decimal) -> PositionState:
        if not amount.is_finite():
            raise ValueError("funding amount must be finite")
        if self.is_flat:
            raise ValueError("cannot apply funding to a flat position")
        quantity = self.quantity
        lots = tuple(
            replace(
                lot,
                funding=lot.funding + amount * lot.remaining_quantity / quantity,
            )
            if lot.remaining_quantity > 0
            else lot
            for lot in self.lots
        )
        return replace(
            self,
            funding=self.funding + amount,
            lots=lots,
            version=self.version + 1,
        )


def _require_nonnegative(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a nonnegative finite Decimal")
