from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from maais.domain.enums import (
    PaperOrderSide,
    PaperOrderStatus,
    PaperOrderType,
    PositionEffect,
)
from maais.domain.json import JsonValue, freeze_json
from maais.execution.paper.clock import require_utc
from maais.execution.paper.market import BookSnapshot, TradePrint, require_positive_decimal

TERMINAL_ORDER_STATUSES = {
    PaperOrderStatus.FILLED,
    PaperOrderStatus.CANCELED,
    PaperOrderStatus.REJECTED,
    PaperOrderStatus.EXPIRED,
}


class IllegalOrderTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LimitFill:
    market_event_id: str
    fill_at: datetime
    quantity: Decimal
    price: Decimal
    fee: Decimal


@dataclass(frozen=True, slots=True)
class LimitQueueState:
    order_id: UUID
    symbol: str
    side: PaperOrderSide
    limit_price: Decimal
    requested_quantity: Decimal
    filled_quantity: Decimal
    queue_ahead: Decimal
    eligible_after: datetime
    expires_at: datetime
    maker_fee_rate: Decimal
    status: PaperOrderStatus
    last_sequence: int

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity

    @classmethod
    def from_book(
        cls,
        *,
        order_id: UUID,
        side: PaperOrderSide,
        limit_price: Decimal,
        quantity: Decimal,
        eligible_after: datetime,
        expires_at: datetime,
        maker_fee_rate: Decimal,
        book: BookSnapshot,
    ) -> LimitQueueState:
        require_positive_decimal(limit_price, "limit_price")
        require_positive_decimal(quantity, "quantity")
        require_utc(eligible_after, "eligible_after")
        require_utc(expires_at, "expires_at")
        if expires_at <= eligible_after:
            raise ValueError("expires_at must follow eligible_after")
        if (
            not isinstance(maker_fee_rate, Decimal)
            or not maker_fee_rate.is_finite()
            or maker_fee_rate < 0
            or maker_fee_rate > Decimal("1")
        ):
            raise ValueError("maker_fee_rate must be a finite Decimal in [0, 1]")
        resting_levels = book.bids if side is PaperOrderSide.BUY else book.asks
        queue_ahead = next(
            (level.quantity for level in resting_levels if level.price == limit_price),
            Decimal("0"),
        )
        return cls(
            order_id=order_id,
            symbol=book.symbol,
            side=side,
            limit_price=limit_price,
            requested_quantity=quantity,
            filled_quantity=Decimal("0"),
            queue_ahead=queue_ahead,
            eligible_after=eligible_after,
            expires_at=expires_at,
            maker_fee_rate=maker_fee_rate,
            status=PaperOrderStatus.ACCEPTED,
            last_sequence=book.sequence,
        )


@dataclass(frozen=True, slots=True)
class LimitQueueAdvance:
    state: LimitQueueState
    fills: tuple[LimitFill, ...]


def advance_limit_queue(state: LimitQueueState, trade: TradePrint) -> LimitQueueAdvance:
    if state.status in TERMINAL_ORDER_STATUSES:
        return LimitQueueAdvance(state, ())
    if trade.symbol != state.symbol:
        return LimitQueueAdvance(state, ())
    if trade.observed_at >= state.expires_at:
        return LimitQueueAdvance(
            replace(state, status=PaperOrderStatus.EXPIRED, last_sequence=trade.sequence),
            (),
        )
    if trade.observed_at <= state.eligible_after or trade.sequence <= state.last_sequence:
        return LimitQueueAdvance(state, ())
    qualifies = (
        state.side is PaperOrderSide.BUY
        and trade.aggressor_side == "sell"
        and trade.price < state.limit_price
    ) or (
        state.side is PaperOrderSide.SELL
        and trade.aggressor_side == "buy"
        and trade.price > state.limit_price
    )
    if not qualifies:
        return LimitQueueAdvance(replace(state, last_sequence=trade.sequence), ())

    trade_remaining = trade.quantity
    queue_ahead = state.queue_ahead
    if queue_ahead > 0:
        consumed = min(queue_ahead, trade_remaining)
        queue_ahead -= consumed
        trade_remaining -= consumed
    fill_quantity = min(state.remaining_quantity, trade_remaining * Decimal("0.10"))
    if fill_quantity <= 0:
        return LimitQueueAdvance(
            replace(state, queue_ahead=queue_ahead, last_sequence=trade.sequence),
            (),
        )
    cumulative = state.filled_quantity + fill_quantity
    status = (
        PaperOrderStatus.FILLED
        if cumulative == state.requested_quantity
        else PaperOrderStatus.PARTIALLY_FILLED
    )
    next_state = replace(
        state,
        filled_quantity=cumulative,
        queue_ahead=queue_ahead,
        status=status,
        last_sequence=trade.sequence,
    )
    fill = LimitFill(
        market_event_id=trade.event_id,
        fill_at=trade.observed_at,
        quantity=fill_quantity,
        price=trade.price,
        fee=fill_quantity * trade.price * state.maker_fee_rate,
    )
    return LimitQueueAdvance(next_state, (fill,))


@dataclass(frozen=True, slots=True)
class OrderTransition:
    sequence: int
    event_type: str
    event_at: datetime
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: UUID
    experiment_id: UUID
    proposal_id: UUID
    client_order_id: str
    command_hash: str
    symbol: str
    side: PaperOrderSide
    order_type: PaperOrderType
    position_effect: PositionEffect
    quantity: Decimal
    filled_quantity: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    status: PaperOrderStatus
    created_at: datetime
    expires_at: datetime
    version: int
    events: tuple[OrderTransition, ...]

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @classmethod
    def create(
        cls,
        *,
        order_id: UUID,
        experiment_id: UUID,
        proposal_id: UUID,
        client_order_id: str,
        command_hash: str,
        symbol: str,
        side: PaperOrderSide,
        order_type: PaperOrderType,
        position_effect: PositionEffect,
        quantity: Decimal,
        limit_price: Decimal | None,
        reduce_only: bool,
        open_quantity: Decimal,
        created_at: datetime,
        expires_at: datetime,
    ) -> PaperOrder:
        if not client_order_id or not symbol:
            raise ValueError("client_order_id and symbol are required")
        if len(command_hash) != 64:
            raise ValueError("command_hash must be a SHA-256 hex digest")
        require_positive_decimal(quantity, "quantity")
        require_utc(created_at, "created_at")
        require_utc(expires_at, "expires_at")
        if expires_at <= created_at:
            raise ValueError("expires_at must follow created_at")
        if order_type is PaperOrderType.LIMIT:
            if limit_price is None:
                raise ValueError("limit orders require limit_price")
            require_positive_decimal(limit_price, "limit_price")
        elif limit_price is not None:
            raise ValueError("non-limit orders cannot have limit_price")
        if position_effect is PositionEffect.REDUCE:
            if not reduce_only:
                raise ValueError("reduce position effect requires reduce_only")
            if quantity > open_quantity:
                raise ValueError("reduce-only quantity exceeds open quantity")
        elif reduce_only:
            raise ValueError("open position effect cannot be reduce_only")
        payload = freeze_json(
            {
                "client_order_id": client_order_id,
                "command_hash": command_hash,
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "position_effect": position_effect,
                "quantity": quantity,
                "limit_price": limit_price,
                "reduce_only": reduce_only,
            }
        )
        assert isinstance(payload, Mapping)
        event = OrderTransition(1, "order.created", created_at, payload)
        return cls(
            order_id=order_id,
            experiment_id=experiment_id,
            proposal_id=proposal_id,
            client_order_id=client_order_id,
            command_hash=command_hash,
            symbol=symbol,
            side=side,
            order_type=order_type,
            position_effect=position_effect,
            quantity=quantity,
            filled_quantity=Decimal("0"),
            limit_price=limit_price,
            reduce_only=reduce_only,
            status=PaperOrderStatus.CREATED,
            created_at=created_at,
            expires_at=expires_at,
            version=1,
            events=(event,),
        )

    def _transition(
        self,
        status: PaperOrderStatus,
        event_type: str,
        event_at: datetime,
        payload: object = (),
    ) -> PaperOrder:
        require_utc(event_at, "event_at")
        if self.status in TERMINAL_ORDER_STATUSES:
            raise IllegalOrderTransition(f"order is terminal in {self.status.value}")
        frozen = freeze_json(payload)
        if not isinstance(frozen, Mapping):
            frozen = freeze_json({"value": frozen})
        assert isinstance(frozen, Mapping)
        event = OrderTransition(self.version + 1, event_type, event_at, frozen)
        return replace(
            self,
            status=status,
            version=self.version + 1,
            events=(*self.events, event),
        )

    def authorize(self, event_at: datetime) -> PaperOrder:
        if self.status is not PaperOrderStatus.CREATED:
            raise IllegalOrderTransition("only created orders can be authorized")
        return self._transition(PaperOrderStatus.AUTHORIZED, "order.authorized", event_at)

    def accept(self, event_at: datetime) -> PaperOrder:
        if self.status is not PaperOrderStatus.AUTHORIZED:
            raise IllegalOrderTransition("only authorized orders can be accepted")
        return self._transition(PaperOrderStatus.ACCEPTED, "order.accepted", event_at)

    def apply_fill(self, quantity: Decimal, event_at: datetime) -> PaperOrder:
        if self.status not in {
            PaperOrderStatus.ACCEPTED,
            PaperOrderStatus.PARTIALLY_FILLED,
        }:
            raise IllegalOrderTransition("order cannot fill from current status")
        require_positive_decimal(quantity, "fill quantity")
        if quantity > self.remaining_quantity:
            raise ValueError("fill quantity exceeds remaining quantity")
        next_filled = self.filled_quantity + quantity
        next_status = (
            PaperOrderStatus.FILLED
            if next_filled == self.quantity
            else PaperOrderStatus.PARTIALLY_FILLED
        )
        transitioned = self._transition(
            next_status,
            "order.filled" if next_status is PaperOrderStatus.FILLED else "order.partially_filled",
            event_at,
            {"quantity": quantity, "cumulative_quantity": next_filled},
        )
        return replace(transitioned, filled_quantity=next_filled)

    def cancel(self, event_at: datetime) -> PaperOrder:
        return self._transition(PaperOrderStatus.CANCELED, "order.canceled", event_at)

    def expire(self, event_at: datetime) -> PaperOrder:
        return self._transition(PaperOrderStatus.EXPIRED, "order.expired", event_at)

    def reject(self, event_at: datetime, reason: str) -> PaperOrder:
        if not reason:
            raise ValueError("rejection reason is required")
        return self._transition(
            PaperOrderStatus.REJECTED,
            "order.rejected",
            event_at,
            {"reason": reason},
        )
