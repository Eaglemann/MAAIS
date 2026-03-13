"""SQLAlchemy ORM models for the Compliance Engine (Batch 2).

Tables:
  trade_lots          — open FIFO position lots (Rule 9)
  trade_records       — completed trades: all 11 Rule 8 fields
  post_trade_reasoning — decision context for each trade (supports Batch 9 learning)
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base


class TradeLot(Base):
    """Open position lot — the FIFO unit. Created on open, consumed on close."""
    __tablename__ = "trade_lots"
    __table_args__ = (
        sa.Index("ix_lots_asset_side_opened", "asset", "side", "opened_at"),
    )

    trade_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)   # UUID
    asset: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    side: Mapped[str] = mapped_column(sa.String(5), nullable=False)           # "long"|"short"
    entry_price: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    original_size: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)
    remaining_size: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)
    entry_fees: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    strategy_id: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<TradeLot {self.trade_id[:8]} {self.asset} {self.side} remaining={self.remaining_size}>"


class TradeRecord(Base):
    """Completed trade record — all 11 mandatory fields (Rule 8, BEHAVIOURS.md)."""
    __tablename__ = "trade_records"
    __table_args__ = (
        sa.Index("ix_trades_asset_ts", "asset", "timestamp"),
        sa.Index("ix_trades_strategy", "strategy_id"),
    )

    # Rule 8 — field 1
    trade_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    # Rule 8 — field 2
    timestamp: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Rule 8 — field 3
    asset: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    # Rule 8 — field 4
    entry_price: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    # Rule 8 — field 5
    exit_price: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    # Rule 8 — field 6
    position_size: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)
    # Rule 8 — field 7
    fees: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    # Rule 8 — field 8
    funding_paid_received: Mapped[Decimal] = mapped_column(sa.Numeric(20, 10), nullable=False)
    # Rule 8 — field 9
    profit_loss: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    # Rule 8 — field 10
    exchange_rate: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    # Rule 8 — field 11
    strategy_id: Mapped[str] = mapped_column(sa.String(100), nullable=False)

    # Non-mandatory metadata
    position_side: Mapped[str] = mapped_column(sa.String(5), nullable=False)
    entry_lot_id: Mapped[str] = mapped_column(sa.String(36), nullable=False, default="")

    def __repr__(self) -> str:
        return f"<TradeRecord {self.trade_id[:8]} {self.asset} P&L={self.profit_loss}>"


class PostTradeReasoning(Base):
    """Agent outputs and EV calculation stored with every trade (supports Batch 9)."""
    __tablename__ = "post_trade_reasoning"
    __table_args__ = (
        sa.Index("ix_reasoning_trade", "trade_id"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(
        sa.String(36), sa.ForeignKey("trade_records.trade_id"), nullable=False
    )
    agent_outputs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    consensus: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reasoning_summary: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<PostTradeReasoning trade={self.trade_id[:8]}>"
