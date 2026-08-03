"""Execution Engine — orchestrates order placement, fill tracking, and compliance.

Pipeline:
  1. Pre-flight: leverage check (Rule 20), set leverage if needed
  2. Place order (market / limit / stop-limit)
  3. Wait for fill (FillTracker)
  4. Cost overrun check (Rule 20): actual fee > 2× estimated → flag warning
  5. Record trade (TradeRecordWriter)
  6. Return OrderResult
"""

from __future__ import annotations

from decimal import Decimal

from maais.core.logging import get_logger
from maais.decision.schemas import DecisionResult
from maais.execution.fill_tracker import FillTracker
from maais.execution.funding_tracker import FundingTracker
from maais.execution.leverage import LeverageEnforcer
from maais.execution.protocols import AuthenticatedExecutionClient
from maais.execution.schemas import OrderRequest, OrderResult, OrderSide, OrderStatus, OrderType
from maais.execution.trade_record_writer import TradeRecordWriter
from maais.risk.schemas import PositionSize

logger = get_logger(__name__)

# Rule 20: flag if actual cost > 2× estimated
_COST_OVERRUN_MULTIPLIER = 2.0


class ExecutionEngine:
    """Executes approved trades on Binance Futures with full compliance recording."""

    def __init__(
        self,
        client: AuthenticatedExecutionClient,
        leverage_enforcer: LeverageEnforcer,
        fill_tracker: FillTracker,
        funding_tracker: FundingTracker,
        trade_record_writer: TradeRecordWriter,
        target_leverage: int = 1,
    ) -> None:
        self._client = client
        self._leverage = leverage_enforcer
        self._fills = fill_tracker
        self._funding = funding_tracker
        self._writer = trade_record_writer
        self._target_leverage = target_leverage

    async def execute(
        self,
        request: OrderRequest,
        position_size: PositionSize,
        decision: DecisionResult,
        *,
        monitoring_approved: bool,
        reference_price: Decimal,
        is_closing: bool = False,
    ) -> OrderResult:
        """Execute a trade order end-to-end.

        Args:
            request: The order to place.
            position_size: Approved position size from Risk Engine.
            decision: DecisionResult from Decision Engine (for cost comparison).
            monitoring_approved: Current monitoring and kill-switch gate result.
            reference_price: Current price used to validate market-order notional.
            is_closing: True if this order closes an existing position.

        Returns:
            OrderResult with fill details and compliance flags.
        """
        estimated_cost = decision.costs.total_cost_pct

        # Signed execution is a fail-closed protocol-smoke boundary. Every
        # upstream approval must be explicit before a client method is called.
        if not decision.approved:
            return self._rejected(request, estimated_cost, "decision_not_approved")
        if not position_size.approved:
            return self._rejected(request, estimated_cost, "risk_not_approved")
        if not monitoring_approved:
            return self._rejected(request, estimated_cost, "monitoring_not_approved")
        if request.symbol != decision.symbol or request.symbol != position_size.symbol:
            return self._rejected(request, estimated_cost, "symbol_mismatch")
        if not request.quantity.is_finite() or request.quantity <= 0:
            return self._rejected(request, estimated_cost, "invalid_quantity")
        if not reference_price.is_finite() or reference_price <= 0:
            return self._rejected(request, estimated_cost, "invalid_reference_price")
        if is_closing != request.reduce_only:
            return self._rejected(request, estimated_cost, "closing_flag_mismatch")

        if not is_closing:
            expected_side = {
                "long": OrderSide.BUY,
                "short": OrderSide.SELL,
            }.get(decision.direction)
            if expected_side is None or request.side is not expected_side:
                return self._rejected(request, estimated_cost, "decision_side_mismatch")

        validation_price = (
            request.price if request.order_type is OrderType.LIMIT else reference_price
        )
        if validation_price is None or not validation_price.is_finite() or validation_price <= 0:
            return self._rejected(request, estimated_cost, "invalid_order_price")
        approved_notional = Decimal(str(position_size.final_usd))
        if not approved_notional.is_finite() or approved_notional <= 0:
            return self._rejected(request, estimated_cost, "invalid_approved_notional")
        requested_notional = request.quantity * validation_price
        if not request.reduce_only and requested_notional > approved_notional:
            return self._rejected(request, estimated_cost, "approved_notional_exceeded")

        # 1. Leverage pre-flight check
        allowed, reason = self._leverage.validate(request.symbol, self._target_leverage)
        if not allowed:
            return OrderResult(
                request=request,
                order_id=None,
                status=OrderStatus.REJECTED,
                fill=None,
                leverage_used=self._target_leverage,
                estimated_cost_pct=estimated_cost,
                actual_cost_pct=None,
                cost_overrun_flagged=False,
                error_message=reason,
                approved=False,
                compliance_recorded=False,
            )

        # 2. Set leverage if needed
        if self._leverage.needs_update(request.symbol, self._target_leverage):
            confirmed_leverage = await self._client.set_leverage(
                request.symbol, self._target_leverage
            )
            self._leverage.record_set(request.symbol, confirmed_leverage)
        else:
            confirmed_leverage = self._target_leverage

        # 3. Place order
        try:
            raw = await self._client.place_order(request)
            order_id = str(raw["orderId"])
        except Exception as exc:
            logger.error("order_placement_failed", symbol=request.symbol, error=str(exc))
            return OrderResult(
                request=request,
                order_id=None,
                status=OrderStatus.REJECTED,
                fill=None,
                leverage_used=confirmed_leverage,
                estimated_cost_pct=estimated_cost,
                actual_cost_pct=None,
                cost_overrun_flagged=False,
                error_message=str(exc),
                approved=False,
                compliance_recorded=False,
            )

        # 4. Wait for fill
        status, fill = await self._fills.wait_for_fill(request.symbol, order_id)

        # 5. Cost overrun check (Rule 20)
        actual_cost_pct = None
        cost_overrun_flagged = False
        if fill is not None and fill.avg_fill_price > 0 and fill.filled_quantity > 0:
            notional = fill.avg_fill_price * fill.filled_quantity
            actual_cost_pct = float(fill.commission / notional) if notional > 0 else None
            if actual_cost_pct and actual_cost_pct > estimated_cost * _COST_OVERRUN_MULTIPLIER:
                cost_overrun_flagged = True
                logger.warning(
                    "cost_overrun_detected",
                    symbol=request.symbol,
                    estimated=f"{estimated_cost:.4%}",
                    actual=f"{actual_cost_pct:.4%}",
                )

        # 6. Record trade
        compliance_recorded = status is not OrderStatus.FILLED
        compliance_error: str | None = None
        if fill is not None and status == OrderStatus.FILLED:
            try:
                if is_closing:
                    await self._writer.record_close(fill, request.strategy_id)
                else:
                    await self._writer.record_open(fill, request.strategy_id)
                compliance_recorded = True
            except Exception as exc:
                logger.error("trade_record_failed", symbol=request.symbol, error=str(exc))
                compliance_error = f"post_fill_recording_failed: {exc}"

        return OrderResult(
            request=request,
            order_id=order_id,
            status=status,
            fill=fill,
            leverage_used=confirmed_leverage,
            estimated_cost_pct=estimated_cost,
            actual_cost_pct=actual_cost_pct,
            cost_overrun_flagged=cost_overrun_flagged,
            error_message=compliance_error,
            approved=compliance_error is None,
            compliance_recorded=compliance_recorded,
        )

    def _rejected(
        self,
        request: OrderRequest,
        estimated_cost: float,
        reason: str,
    ) -> OrderResult:
        return OrderResult(
            request=request,
            order_id=None,
            status=OrderStatus.REJECTED,
            fill=None,
            leverage_used=self._target_leverage,
            estimated_cost_pct=estimated_cost,
            actual_cost_pct=None,
            cost_overrun_flagged=False,
            error_message=reason,
            approved=False,
            compliance_recorded=False,
        )
