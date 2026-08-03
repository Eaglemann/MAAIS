"""Tests for the Execution Engine (Batch 7).

Binance API calls are mocked — no live network required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maais.compliance.fifo_ledger import FifoLedger
from maais.config.constants import MAX_LEVERAGE
from maais.decision.schemas import (
    AdversarialSummary,
    ConsensusResult,
    CostEstimate,
    DecisionResult,
    EVResult,
)
from maais.execution.fill_tracker import FillTracker, _parse_fill
from maais.execution.funding_tracker import FundingTracker
from maais.execution.leverage import LeverageEnforcer
from maais.execution.schemas import (
    FillRecord,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from maais.execution.trade_record_writer import TradeRecordWriter
from maais.risk.schemas import PositionSize

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _fill(
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    qty: str = "0.01",
    price: str = "50000.0",
    commission: str = "1.0",
) -> FillRecord:
    return FillRecord(
        order_id="123",
        symbol=symbol,
        side=side,
        filled_quantity=Decimal(qty),
        avg_fill_price=Decimal(price),
        commission=Decimal(commission),
        commission_asset="USDT",
        fill_time=_ts(),
        realized_pnl=Decimal("0"),
    )


def _order_request(
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    qty: str = "0.01",
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=Decimal(qty),
        price=None,
        stop_price=None,
        strategy_id="strat_001",
    )


def _decision(estimated_cost: float = 0.002, approved: bool = True) -> DecisionResult:
    consensus = ConsensusResult("long", 0.7, 0.6, 2.0, 1.0, 0.0, 3, ["a"], ["b"], [])
    adversarial = AdversarialSummary("long", "short", [], 0.5, 0.3, False)
    costs = CostEstimate(0.0008, 0.001, estimated_cost, 0.0)
    ev = EVResult(0.7, 0.3, 0.01, 0.01, 0.004, 0.0, 0.002, True)
    return DecisionResult(
        symbol="BTCUSDT",
        timeframe="1m",
        timestamp=_ts(),
        approved=approved,
        direction="long",
        consensus=consensus,
        adversarial=adversarial,
        costs=costs,
        ev=ev,
    )


def _position_size(approved: bool = True, final_usd: float = 2000.0) -> PositionSize:
    return PositionSize(
        symbol="BTCUSDT",
        capital=100_000.0,
        half_kelly_fraction=0.02,
        vol_adjusted_fraction=0.02,
        risk_cap_fraction=0.02,
        drawdown_multiplier=1.0,
        correlation_multiplier=1.0,
        base_fraction=0.02,
        final_fraction=0.02,
        final_usd=final_usd,
        approved=approved,
        rejection_reason=None,
    )


# ── Constants ─────────────────────────────────────────────────────────────────


class TestConstants:
    def test_max_leverage_is_5(self):
        assert MAX_LEVERAGE == 5


# ── LeverageEnforcer ──────────────────────────────────────────────────────────


class TestLeverageEnforcer:
    def test_within_cap_allowed(self):
        enforcer = LeverageEnforcer()
        ok, reason = enforcer.validate("BTCUSDT", 5)
        assert ok
        assert reason is None

    def test_above_cap_rejected(self):
        enforcer = LeverageEnforcer()
        ok, reason = enforcer.validate("BTCUSDT", 6)
        assert not ok
        assert "leverage_cap_exceeded" in reason

    def test_exactly_at_cap_allowed(self):
        enforcer = LeverageEnforcer()
        ok, _ = enforcer.validate("BTCUSDT", MAX_LEVERAGE)
        assert ok

    def test_needs_update_when_not_set(self):
        enforcer = LeverageEnforcer()
        assert enforcer.needs_update("BTCUSDT", 5)

    def test_no_update_needed_after_record(self):
        enforcer = LeverageEnforcer()
        enforcer.record_set("BTCUSDT", 5)
        assert not enforcer.needs_update("BTCUSDT", 5)

    def test_needs_update_when_different(self):
        enforcer = LeverageEnforcer()
        enforcer.record_set("BTCUSDT", 3)
        assert enforcer.needs_update("BTCUSDT", 5)

    def test_max_leverage_property(self):
        enforcer = LeverageEnforcer(max_leverage=3)
        assert enforcer.max_leverage == 3


# ── OrderRequest / FillRecord schemas ─────────────────────────────────────────


class TestSchemas:
    def test_order_request_market_no_price(self):
        req = _order_request(order_type=OrderType.MARKET)
        assert req.price is None
        assert req.stop_price is None

    def test_fill_record_fields(self):
        fill = _fill()
        assert fill.symbol == "BTCUSDT"
        assert fill.filled_quantity == Decimal("0.01")
        assert fill.avg_fill_price == Decimal("50000.0")

    def test_order_status_enum_values(self):
        assert OrderStatus.FILLED.value == "FILLED"
        assert OrderStatus.CANCELED.value == "CANCELED"
        assert OrderStatus.REJECTED.value == "REJECTED"

    def test_order_type_enum_values(self):
        assert OrderType.MARKET.value == "MARKET"
        assert OrderType.LIMIT.value == "LIMIT"
        assert OrderType.STOP.value == "STOP"


# ── FillTracker ───────────────────────────────────────────────────────────────


class TestFillTracker:
    def _mock_client(self, responses: list[dict]) -> MagicMock:
        """Build a mock client that returns sequential get_order responses."""
        client = MagicMock()
        client.get_order = AsyncMock(side_effect=responses)
        return client

    async def test_immediate_fill(self):
        raw_filled = {
            "orderId": 123,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "0.01",
            "avgPrice": "50000",
            "commission": "1.0",
            "commissionAsset": "USDT",
            "updateTime": 1704067200000,
            "realizedPnl": "0",
        }
        client = self._mock_client([raw_filled])
        tracker = FillTracker(client)
        status, fill = await tracker.wait_for_fill("BTCUSDT", "123")
        assert status == OrderStatus.FILLED
        assert fill is not None
        assert fill.avg_fill_price == Decimal("50000")

    async def test_eventually_filled(self):
        pending = {
            "orderId": 123,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
            "commission": "0",
            "commissionAsset": "USDT",
            "updateTime": 1704067200000,
            "realizedPnl": "0",
        }
        filled = {
            **pending,
            "status": "FILLED",
            "executedQty": "0.01",
            "avgPrice": "50000",
            "commission": "1.0",
        }
        client = self._mock_client([pending, filled])
        tracker = FillTracker(client)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            status, fill = await tracker.wait_for_fill("BTCUSDT", "123")
        assert status == OrderStatus.FILLED
        assert fill is not None

    async def test_canceled_returns_no_fill(self):
        raw_canceled = {
            "orderId": 123,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "CANCELED",
            "executedQty": "0",
            "avgPrice": "0",
            "commission": "0",
            "commissionAsset": "USDT",
            "updateTime": 1704067200000,
            "realizedPnl": "0",
        }
        client = self._mock_client([raw_canceled])
        tracker = FillTracker(client)
        status, fill = await tracker.wait_for_fill("BTCUSDT", "123")
        assert status == OrderStatus.CANCELED
        assert fill is None

    def test_parse_fill_extracts_fields(self):
        raw = {
            "orderId": 456,
            "symbol": "ETHUSDT",
            "side": "SELL",
            "executedQty": "1.0",
            "avgPrice": "3000.0",
            "commission": "1.2",
            "commissionAsset": "USDT",
            "updateTime": 1704067200000,
            "realizedPnl": "50.0",
        }
        fill = _parse_fill(raw)
        assert fill.symbol == "ETHUSDT"
        assert fill.side == OrderSide.SELL
        assert fill.avg_fill_price == Decimal("3000.0")
        assert fill.realized_pnl == Decimal("50.0")


# ── FundingTracker ────────────────────────────────────────────────────────────


class TestFundingTracker:
    async def test_refresh_sums_payments(self):
        client = MagicMock()
        client.get_funding_payments = AsyncMock(
            return_value=[
                {"income": "1.5"},
                {"income": "-0.5"},
            ]
        )
        tracker = FundingTracker(client)
        total = await tracker.refresh("BTCUSDT")
        assert total == Decimal("1.0")

    async def test_get_cumulative_before_refresh_zero(self):
        tracker = FundingTracker(MagicMock())
        assert tracker.get_cumulative("BTCUSDT") == Decimal("0")

    async def test_get_cumulative_after_refresh(self):
        client = MagicMock()
        client.get_funding_payments = AsyncMock(return_value=[{"income": "3.0"}])
        tracker = FundingTracker(client)
        await tracker.refresh("BTCUSDT")
        assert tracker.get_cumulative("BTCUSDT") == Decimal("3.0")

    async def test_empty_payments_returns_zero(self):
        client = MagicMock()
        client.get_funding_payments = AsyncMock(return_value=[])
        tracker = FundingTracker(client)
        total = await tracker.refresh("BTCUSDT")
        assert total == Decimal("0")


# ── TradeRecordWriter ─────────────────────────────────────────────────────────


class TestTradeRecordWriter:
    def _writer(self) -> tuple[TradeRecordWriter, FifoLedger, MagicMock]:
        ledger = FifoLedger()
        trade_logger = MagicMock()
        trade_logger.save_lot = AsyncMock()
        trade_logger.save_trade_records = AsyncMock()
        rate_fetcher = MagicMock()
        rate_fetcher.get_rate = AsyncMock(return_value=Decimal("1.0"))
        funding_tracker = MagicMock()
        funding_tracker.get_cumulative = MagicMock(return_value=Decimal("0.5"))
        writer = TradeRecordWriter(
            ledger=ledger,
            trade_logger=trade_logger,
            rate_fetcher=rate_fetcher,
            funding_tracker=funding_tracker,
        )
        return writer, ledger, trade_logger

    async def test_record_open_creates_lot(self):
        writer, ledger, logger = self._writer()
        fill = _fill(side=OrderSide.BUY)
        await writer.record_open(fill)
        assert ledger.get_open_size("BTCUSDT", "long") == Decimal("0.01")
        logger.save_lot.assert_called_once()

    async def test_record_open_uses_fill_data(self):
        writer, ledger, _ = self._writer()
        fill = _fill(side=OrderSide.BUY, qty="0.05", price="48000")
        await writer.record_open(fill)
        lots = ledger.get_open_lots("BTCUSDT", "long")
        assert len(lots) == 1
        assert lots[0].entry_price == Decimal("48000")
        assert lots[0].original_size == Decimal("0.05")

    async def test_record_close_after_open(self):
        writer, ledger, logger = self._writer()
        # Open a long lot first
        open_fill = _fill(side=OrderSide.BUY, qty="0.01", price="50000")
        await writer.record_open(open_fill)
        # Close it with a SELL
        close_fill = _fill(side=OrderSide.SELL, qty="0.01", price="51000")
        records = await writer.record_close(close_fill)
        assert len(records) == 1
        assert records[0].exit_price == Decimal("51000")
        logger.save_trade_records.assert_called_once()

    async def test_record_close_long_with_profit(self):
        writer, ledger, _ = self._writer()
        await writer.record_open(
            _fill(side=OrderSide.BUY, qty="0.01", price="50000", commission="0")
        )
        records = await writer.record_close(
            _fill(side=OrderSide.SELL, qty="0.01", price="52000", commission="0")
        )
        # P&L = (52000 - 50000) × 0.01 = 20 (before fees)
        assert records[0].profit_loss > Decimal("0")


# ── ExecutionEngine (integration with mocks) ──────────────────────────────────


class TestExecutionEngine:
    def _build_engine(
        self,
        fill_status: str = "FILLED",
        avg_price: str = "50000",
        commission: str = "1.0",
        leverage_ok: bool = True,
    ):
        from maais.compliance.fifo_ledger import FifoLedger
        from maais.execution.engine import ExecutionEngine

        raw_order = {"orderId": 789}
        raw_fill = {
            "orderId": 789,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": fill_status,
            "executedQty": "0.01",
            "avgPrice": avg_price,
            "commission": commission,
            "commissionAsset": "USDT",
            "updateTime": 1704067200000,
            "realizedPnl": "0",
        }

        client = MagicMock()
        client.set_leverage = AsyncMock(return_value=5)
        client.place_order = AsyncMock(return_value=raw_order)
        client.get_order = AsyncMock(return_value=raw_fill)
        client.get_funding_payments = AsyncMock(return_value=[])

        enforcer = LeverageEnforcer(max_leverage=5 if leverage_ok else 2)
        fill_tracker = FillTracker(client)
        funding_tracker = FundingTracker(client)

        ledger = FifoLedger()
        trade_logger = MagicMock()
        trade_logger.save_lot = AsyncMock()
        trade_logger.save_trade_records = AsyncMock()
        rate_fetcher = MagicMock()
        rate_fetcher.get_rate = AsyncMock(return_value=Decimal("1.0"))

        writer = TradeRecordWriter(ledger, trade_logger, rate_fetcher, funding_tracker)
        engine = ExecutionEngine(
            client, enforcer, fill_tracker, funding_tracker, writer, target_leverage=5
        )
        return engine

    async def test_successful_fill_approved(self):
        engine = self._build_engine()
        result = await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert result.approved
        assert result.status == OrderStatus.FILLED
        assert result.fill is not None
        assert result.compliance_recorded

    @pytest.mark.parametrize(
        ("decision_approved", "risk_approved", "monitoring_approved", "error"),
        [
            (False, True, True, "decision_not_approved"),
            (True, False, True, "risk_not_approved"),
            (True, True, False, "monitoring_not_approved"),
        ],
    )
    async def test_execution_fails_closed_before_client_call(
        self,
        decision_approved: bool,
        risk_approved: bool,
        monitoring_approved: bool,
        error: str,
    ):
        engine = self._build_engine()
        result = await engine.execute(
            _order_request(),
            _position_size(approved=risk_approved),
            _decision(approved=decision_approved),
            monitoring_approved=monitoring_approved,
            reference_price=Decimal("50000"),
        )
        assert result.status is OrderStatus.REJECTED
        assert result.error_message == error
        assert not result.compliance_recorded
        engine._client.place_order.assert_not_awaited()

    async def test_execution_rejects_notional_above_approved_size(self):
        engine = self._build_engine()
        result = await engine.execute(
            _order_request(qty="1"),
            _position_size(final_usd=2000.0),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert result.error_message == "approved_notional_exceeded"
        engine._client.place_order.assert_not_awaited()

    async def test_execution_rejects_symbol_mismatch(self):
        engine = self._build_engine()
        result = await engine.execute(
            _order_request(symbol="ETHUSDT"),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("3000"),
        )
        assert result.error_message == "symbol_mismatch"
        engine._client.place_order.assert_not_awaited()

    async def test_filled_order_reports_compliance_persistence_failure(self):
        engine = self._build_engine()
        engine._writer.record_open = AsyncMock(side_effect=RuntimeError("database unavailable"))
        result = await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert result.status is OrderStatus.FILLED
        assert result.fill is not None
        assert not result.approved
        assert not result.compliance_recorded
        assert result.error_message == "post_fill_recording_failed: database unavailable"

    async def test_leverage_violation_rejected(self):
        engine = self._build_engine(leverage_ok=False)
        # target_leverage=5 but enforcer.max_leverage=2 → rejected
        engine._target_leverage = 5
        result = await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert not result.approved
        assert result.status == OrderStatus.REJECTED
        assert "leverage_cap_exceeded" in result.error_message

    async def test_canceled_order_not_flagged_as_overrun(self):
        engine = self._build_engine(fill_status="CANCELED")
        result = await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert result.status == OrderStatus.CANCELED
        assert not result.cost_overrun_flagged

    async def test_cost_overrun_flagged_when_actual_much_higher(self):
        # Commission of 100 on a notional of 50000 × 0.01 = 500 → 20% actual cost
        # Estimated cost is 0.002 → 20% >> 2 × 0.002
        engine = self._build_engine(commission="100.0")
        result = await engine.execute(
            _order_request(),
            _position_size(),
            _decision(estimated_cost=0.002),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert result.cost_overrun_flagged

    async def test_fill_sets_order_id(self):
        engine = self._build_engine()
        result = await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert result.order_id == "789"

    async def test_leverage_cached_after_first_set(self):
        engine = self._build_engine()
        # First call sets leverage
        await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        # Second call: set_leverage should not be called again (cached)
        await engine.execute(
            _order_request(),
            _position_size(),
            _decision(),
            monitoring_approved=True,
            reference_price=Decimal("50000"),
        )
        assert engine._client.set_leverage.call_count == 1
