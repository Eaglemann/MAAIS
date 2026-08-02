"""Async persistence layer for the Compliance Engine.

Responsibilities:
  - Save / update TradeLot records (open position lots)
  - Save TradeRecord rows (completed trades, Rule 8)
  - Save PostTradeReasoning rows
  - Load open lots on startup (for FifoLedger recovery)

All writes use PostgreSQL upsert (merge) so reruns are idempotent.
"""

from maais.compliance.models import PostTradeReasoning, TradeLot, TradeRecord
from maais.compliance.schemas import LotData, PostTradeReasoningData, TradeRecordData
from maais.core.logging import get_logger
from maais.db.connection import get_session

logger = get_logger(__name__)


class TradeLogger:
    """Async trade record persistence.  One instance per process is sufficient."""

    # ── Lot management ────────────────────────────────────────────────────────

    async def save_lot(self, lot: LotData) -> None:
        """Persist a new open lot (called immediately after FifoLedger.open_lot)."""
        async with get_session() as session:
            row = TradeLot(
                trade_id=lot.trade_id,
                asset=lot.asset,
                side=lot.side,
                entry_price=lot.entry_price,
                original_size=lot.original_size,
                remaining_size=lot.remaining_size,
                entry_fees=lot.entry_fees,
                strategy_id=lot.strategy_id,
                opened_at=lot.opened_at,
            )
            await session.merge(row)
        logger.debug("lot_saved", trade_id=lot.trade_id, asset=lot.asset)

    async def update_lot_remaining(self, trade_id: str, remaining_size) -> None:
        """Update the remaining size of a lot after a partial close."""

        from sqlalchemy import update

        async with get_session() as session:
            await session.execute(
                update(TradeLot)
                .where(TradeLot.trade_id == trade_id)
                .values(remaining_size=remaining_size)
            )

    async def load_open_lots(self) -> list[LotData]:
        """Load all lots with remaining_size > 0 — called at startup for ledger recovery."""
        from decimal import Decimal

        from sqlalchemy import select

        async with get_session() as session:
            result = await session.execute(
                select(TradeLot).where(TradeLot.remaining_size > Decimal("0"))
            )
            rows = result.scalars().all()

        lots = [
            LotData(
                trade_id=row.trade_id,
                asset=row.asset,
                side=row.side,
                entry_price=row.entry_price,
                original_size=row.original_size,
                remaining_size=row.remaining_size,
                entry_fees=row.entry_fees,
                strategy_id=row.strategy_id,
                opened_at=row.opened_at,
            )
            for row in rows
        ]
        logger.info("open_lots_loaded", count=len(lots))
        return lots

    # ── Trade record persistence ──────────────────────────────────────────────

    async def save_trade_record(self, trade: TradeRecordData) -> None:
        """Persist a completed trade record (Rule 8, 11 fields)."""
        async with get_session() as session:
            row = TradeRecord(
                trade_id=trade.trade_id,
                timestamp=trade.timestamp,
                asset=trade.asset,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                position_size=trade.position_size,
                fees=trade.fees,
                funding_paid_received=trade.funding_paid_received,
                profit_loss=trade.profit_loss,
                exchange_rate=trade.exchange_rate,
                strategy_id=trade.strategy_id,
                position_side=trade.position_side,
                entry_lot_id=trade.entry_lot_id,
            )
            await session.merge(row)
        logger.info(
            "trade_record_saved",
            trade_id=trade.trade_id,
            asset=trade.asset,
            pnl=str(trade.profit_loss),
        )

    async def save_trade_records(self, trades: list[TradeRecordData]) -> None:
        """Batch-persist multiple completed trade records (one close may produce many)."""
        for trade in trades:
            await self.save_trade_record(trade)

    # ── Post-trade reasoning ──────────────────────────────────────────────────

    async def save_reasoning(self, reasoning: PostTradeReasoningData) -> None:
        """Persist post-trade agent reasoning (populated by Decision Engine, Batch 5)."""
        async with get_session() as session:
            row = PostTradeReasoning(
                trade_id=reasoning.trade_id,
                agent_outputs=reasoning.agent_outputs,
                consensus=reasoning.consensus,
                reasoning_summary=reasoning.reasoning_summary,
                created_at=reasoning.created_at,
            )
            session.add(row)
        logger.debug("reasoning_saved", trade_id=reasoning.trade_id)
