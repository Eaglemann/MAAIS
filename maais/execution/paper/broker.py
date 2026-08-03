from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from maais.domain.enums import PaperOrderSide, PaperOrderType, PositionEffect
from maais.domain.json import JsonValue, content_hash, freeze_json
from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import ExecutionAuthorizer, ExecutionCapability
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.exits import ExitIntent, ExitPlan, ExitPlanStatus
from maais.execution.paper.fills import (
    FillRejection,
    MarketFillEngine,
    MarketFillRequest,
    PaperFill,
)
from maais.execution.paper.filters import ExchangeFilterSnapshot, FilterRejection
from maais.execution.paper.market import BookSnapshot
from maais.execution.paper.orders import PaperOrder
from maais.execution.paper.records import ExecutionFillRecord, PaperExecutionRecord


@dataclass(frozen=True, slots=True)
class MarketEntryCommand:
    order_id: UUID
    fill_id: UUID
    position_id: UUID
    exit_plan_id: UUID
    experiment_id: UUID
    decision_cycle_id: UUID
    proposal_id: UUID
    gate_chain_hash: str
    client_order_id: str
    symbol: str
    side: PaperOrderSide
    requested_quantity: Decimal
    approved_quantity: Decimal
    approved_notional: Decimal
    decision_executable_price: Decimal
    decision_completed_at: datetime
    execution_latency: timedelta
    created_at: datetime
    expires_at: datetime
    taker_fee_rate: Decimal
    expected_loss_fraction: Decimal
    expected_gain_fraction: Decimal
    capability: ExecutionCapability
    exchange_filters: ExchangeFilterSnapshot

    def __post_init__(self) -> None:
        if len(self.gate_chain_hash) != 64:
            raise ValueError("gate_chain_hash must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class PaperBrokerResult:
    record: PaperExecutionRecord
    fill: PaperFill


@dataclass(frozen=True, slots=True)
class MarketExitCommand:
    order_id: UUID
    fill_id: UUID
    experiment_id: UUID
    proposal_id: UUID
    client_order_id: str
    symbol: str
    decision_executable_price: Decimal
    execution_latency: timedelta
    created_at: datetime
    expires_at: datetime
    taker_fee_rate: Decimal
    intent: ExitIntent
    exchange_filters: ExchangeFilterSnapshot


class ExitExecutionHalt(RuntimeError):
    """Deterministic protective-exit incident for persistence by the orchestrator."""

    def __init__(
        self,
        command: MarketExitCommand,
        exit_plan: ExitPlan,
        reason: str,
        *,
        market_event_id: str | None = None,
    ) -> None:
        self.command = command
        self.exit_plan = exit_plan
        self.reason = reason
        self.market_event_id = market_event_id
        super().__init__(reason)

    def event_payload(self) -> Mapping[str, JsonValue]:
        payload = freeze_json(
            {
                "experiment_id": self.command.experiment_id,
                "proposal_id": self.command.proposal_id,
                "position_id": self.command.intent.position_id,
                "order_id": self.command.order_id,
                "exit_plan_id": self.exit_plan.plan_id,
                "exit_plan_version": self.exit_plan.version,
                "reason": self.reason,
                "market_event_id": self.market_event_id,
                "triggered_at": self.command.intent.triggered_at,
                "trigger_price": self.command.intent.trigger_price,
                "requires_operator_review": True,
            }
        )
        assert isinstance(payload, Mapping)
        return payload


class PaperBroker:
    """Deterministic local broker for approved market entries."""

    def __init__(
        self,
        *,
        clock: DeterministicClock,
        authorizer: ExecutionAuthorizer,
        market_fills: MarketFillEngine,
    ) -> None:
        self._clock = clock
        self._authorizer = authorizer
        self._market_fills = market_fills

    def execute_market_entry(
        self,
        command: MarketEntryCommand,
        *,
        account: AccountState,
        books: tuple[BookSnapshot, ...],
        active_exit_plan: ExitPlan | None = None,
    ) -> PaperBrokerResult:
        if account.experiment_id != command.experiment_id:
            raise ValueError("account and command experiment differ")
        if command.exchange_filters.captured_at > command.decision_completed_at:
            raise ValueError("exchange filter snapshot cannot be captured after the decision")
        if not self._authorizer.verify(command.capability, at=command.created_at):
            raise PermissionError("execution capability is invalid or expired")
        prepared = command.exchange_filters.prepare(
            side=command.side,
            order_type=PaperOrderType.MARKET,
            requested_quantity=command.requested_quantity,
            approved_quantity=command.approved_quantity,
            price=command.decision_executable_price,
            approved_notional=command.approved_notional,
        )
        claims = command.capability.claims
        expected_claims = (
            command.experiment_id,
            command.decision_cycle_id,
            command.proposal_id,
            command.gate_chain_hash,
            command.symbol,
            command.side,
            prepared.quantity,
            command.approved_notional,
        )
        actual_claims = (
            claims.experiment_id,
            claims.decision_cycle_id,
            claims.proposal_id,
            claims.gate_chain_hash,
            claims.symbol,
            claims.side,
            claims.quantity,
            claims.approved_notional,
        )
        if actual_claims != expected_claims:
            raise PermissionError("execution capability does not match prepared order")
        eligibility = self._clock.eligibility(
            command.decision_completed_at,
            command.execution_latency,
        )
        order = PaperOrder.create(
            order_id=command.order_id,
            experiment_id=command.experiment_id,
            proposal_id=command.proposal_id,
            client_order_id=command.client_order_id,
            command_hash=content_hash(
                {
                    "experiment_id": command.experiment_id,
                    "decision_cycle_id": command.decision_cycle_id,
                    "proposal_id": command.proposal_id,
                    "gate_chain_hash": command.gate_chain_hash,
                    "symbol": command.symbol,
                    "side": command.side,
                    "quantity": prepared.quantity,
                    "approved_notional": command.approved_notional,
                    "eligible_after": eligibility.eligible_at,
                    "expires_at": command.expires_at,
                    "filter_snapshot": {
                        "captured_at": command.exchange_filters.captured_at,
                        "price_tick": command.exchange_filters.price_tick,
                        "quantity_step": command.exchange_filters.quantity_step,
                    },
                }
            ),
            symbol=command.symbol,
            side=command.side,
            order_type=PaperOrderType.MARKET,
            position_effect=PositionEffect.OPEN,
            quantity=prepared.quantity,
            limit_price=None,
            reduce_only=False,
            open_quantity=(
                account.position(command.symbol).quantity
                if command.symbol in account.positions
                else Decimal("0")
            ),
            created_at=command.created_at,
            expires_at=command.expires_at,
        )
        order = order.authorize(command.created_at).accept(command.created_at)
        fill = self._market_fills.fill(
            MarketFillRequest(
                symbol=command.symbol,
                side=command.side,
                quantity=prepared.quantity,
                eligible_after=eligibility.eligible_at,
                decision_executable_price=command.decision_executable_price,
                taker_fee_rate=command.taker_fee_rate,
            ),
            books,
        )
        if fill.fill_at >= command.expires_at:
            raise RuntimeError("first eligible market fill occurred after order expiry")
        order = order.apply_fill(fill.quantity, fill.fill_at)
        next_account = account.apply_fill(
            fill_id=command.fill_id,
            position_id=command.position_id,
            symbol=command.symbol,
            side=command.side,
            position_effect=PositionEffect.OPEN,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            fill_at=fill.fill_at,
        )
        position = next_account.position(command.symbol)
        if active_exit_plan is None:
            exit_plan = ExitPlan.create(
                plan_id=command.exit_plan_id,
                position_id=position.position_id,
                side=position.side,
                quantity=position.quantity,
                average_entry=position.average_entry,
                expected_loss_fraction=command.expected_loss_fraction,
                expected_gain_fraction=command.expected_gain_fraction,
                created_at=fill.fill_at,
            )
        else:
            if active_exit_plan.position_id != position.position_id:
                raise ValueError("active exit plan belongs to another position")
            exit_plan = active_exit_plan.resize(
                quantity=position.quantity,
                average_entry=position.average_entry,
                changed_at=fill.fill_at,
            )
        market_snapshot = freeze_json(
            {
                "book_event_id": fill.book.event_id,
                "venue_event_at": fill.book.venue_event_at,
                "observed_at": fill.book.observed_at,
                "sequence": fill.book.sequence,
                "best_bid": fill.book.best_bid,
                "best_ask": fill.book.best_ask,
                "mark_price": fill.book.mark_price,
                "slices": [
                    {"price": item.price, "quantity": item.quantity} for item in fill.slices
                ],
            }
        )
        assert isinstance(market_snapshot, Mapping)
        typed_market_snapshot: Mapping[str, JsonValue] = market_snapshot
        stored_fill = ExecutionFillRecord(
            id=command.fill_id,
            order_intent_id=command.order_id,
            market_event_id=fill.market_event_id,
            fill_at=fill.fill_at,
            quantity=fill.quantity,
            price=fill.price,
            liquidity_role=fill.liquidity_role,
            fee=fill.fee,
            fee_asset=account.currency,
            spread_cost=fill.spread_cost,
            depth_slippage=fill.depth_slippage,
            latency_slippage=fill.latency_slippage,
            total_slippage=fill.total_slippage,
            market_snapshot=typed_market_snapshot,
        )
        record = PaperExecutionRecord(
            order=order,
            exchange_filters=command.exchange_filters,
            fills=(stored_fill,),
            account=next_account,
            exit_plan=exit_plan,
        )
        record.validate()
        return PaperBrokerResult(record=record, fill=fill)

    def execute_market_exit(
        self,
        command: MarketExitCommand,
        *,
        account: AccountState,
        exit_plan: ExitPlan,
        books: tuple[BookSnapshot, ...],
    ) -> PaperBrokerResult:
        if account.experiment_id != command.experiment_id:
            raise ValueError("account and exit command experiment differ")
        position = account.position(command.symbol)
        if position.is_flat:
            raise ValueError("exit command has no open position")
        if exit_plan.status is not ExitPlanStatus.TRIGGERED:
            raise PermissionError("exit plan is not triggered")
        if (
            command.intent.position_id != position.position_id
            or exit_plan.position_id != position.position_id
            or command.intent.quantity != position.quantity
            or command.intent.side
            is not (PaperOrderSide.SELL if position.side.value == "long" else PaperOrderSide.BUY)
            or not command.intent.reduce_only
        ):
            raise PermissionError("exit intent does not match the open position and plan")
        if command.exchange_filters.captured_at > command.intent.triggered_at:
            raise ExitExecutionHalt(command, exit_plan, "future_exchange_filter_snapshot")
        try:
            prepared = command.exchange_filters.prepare(
                side=command.intent.side,
                order_type=PaperOrderType.MARKET,
                requested_quantity=command.intent.quantity,
                approved_quantity=position.quantity,
                price=command.decision_executable_price,
                approved_notional=position.quantity * command.decision_executable_price,
            )
        except FilterRejection as exc:
            raise ExitExecutionHalt(
                command,
                exit_plan,
                f"exchange_filter:{exc.reason}",
            ) from exc
        if prepared.quantity != position.quantity:
            raise ExitExecutionHalt(command, exit_plan, "exit_quantization_residual")
        eligibility = self._clock.eligibility(
            command.intent.triggered_at,
            command.execution_latency,
        )
        order = (
            PaperOrder.create(
                order_id=command.order_id,
                experiment_id=command.experiment_id,
                proposal_id=command.proposal_id,
                client_order_id=command.client_order_id,
                command_hash=content_hash(
                    {
                        "position_id": position.position_id,
                        "exit_plan_id": exit_plan.plan_id,
                        "exit_plan_version": exit_plan.version,
                        "reason": command.intent.reason,
                        "quantity": prepared.quantity,
                        "eligible_after": eligibility.eligible_at,
                    }
                ),
                symbol=command.symbol,
                side=command.intent.side,
                order_type=PaperOrderType.MARKET,
                position_effect=PositionEffect.REDUCE,
                quantity=prepared.quantity,
                limit_price=None,
                reduce_only=True,
                open_quantity=position.quantity,
                created_at=command.created_at,
                expires_at=command.expires_at,
            )
            .authorize(command.created_at)
            .accept(command.created_at)
        )
        try:
            fill = self._market_fills.fill(
                MarketFillRequest(
                    symbol=command.symbol,
                    side=command.intent.side,
                    quantity=prepared.quantity,
                    eligible_after=eligibility.eligible_at,
                    decision_executable_price=command.decision_executable_price,
                    taker_fee_rate=command.taker_fee_rate,
                ),
                books,
            )
        except FillRejection as exc:
            raise ExitExecutionHalt(
                command,
                exit_plan,
                exc.reason,
                market_event_id=exc.market_event_id,
            ) from exc
        if fill.fill_at >= command.expires_at:
            raise ExitExecutionHalt(
                command,
                exit_plan,
                "exit_fill_after_expiry",
                market_event_id=fill.market_event_id,
            )
        order = order.apply_fill(fill.quantity, fill.fill_at)
        next_account = account.apply_fill(
            fill_id=command.fill_id,
            position_id=position.position_id,
            symbol=command.symbol,
            side=command.intent.side,
            position_effect=PositionEffect.REDUCE,
            quantity=fill.quantity,
            price=fill.price,
            fee=fill.fee,
            fill_at=fill.fill_at,
        )
        closed_plan = exit_plan.close(fill.fill_at)
        market_snapshot = freeze_json(
            {
                "book_event_id": fill.book.event_id,
                "venue_event_at": fill.book.venue_event_at,
                "observed_at": fill.book.observed_at,
                "sequence": fill.book.sequence,
                "best_bid": fill.book.best_bid,
                "best_ask": fill.book.best_ask,
                "mark_price": fill.book.mark_price,
                "trigger_price": command.intent.trigger_price,
                "exit_reason": command.intent.reason,
                "slices": [
                    {"price": item.price, "quantity": item.quantity} for item in fill.slices
                ],
            }
        )
        assert isinstance(market_snapshot, Mapping)
        typed_market_snapshot: Mapping[str, JsonValue] = market_snapshot
        stored_fill = ExecutionFillRecord(
            id=command.fill_id,
            order_intent_id=command.order_id,
            market_event_id=fill.market_event_id,
            fill_at=fill.fill_at,
            quantity=fill.quantity,
            price=fill.price,
            liquidity_role=fill.liquidity_role,
            fee=fill.fee,
            fee_asset=account.currency,
            spread_cost=fill.spread_cost,
            depth_slippage=fill.depth_slippage,
            latency_slippage=fill.latency_slippage,
            total_slippage=fill.total_slippage,
            market_snapshot=typed_market_snapshot,
        )
        record = PaperExecutionRecord(
            order=order,
            exchange_filters=command.exchange_filters,
            fills=(stored_fill,),
            account=next_account,
            exit_plan=closed_plan,
        )
        record.validate()
        return PaperBrokerResult(record=record, fill=fill)
