"""SQLAlchemy ORM models for the Market Data Layer (Layer 1).

Tables:
  klines              — OHLCV candles for all timeframes and symbols
  funding_rates       — Perpetual futures funding rate history
  order_book_snapshots — Periodic order book depth snapshots
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base


class Kline(Base):
    __tablename__ = "klines"
    __table_args__ = (
        sa.UniqueConstraint("symbol", "timeframe", "open_time", name="uq_klines"),
        sa.Index("ix_klines_symbol_tf_time", "symbol", "timeframe", "open_time"),
        sa.Index("ix_klines_symbol_time", "symbol", "open_time"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(sa.String(5), nullable=False)
    open_time: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)
    close_time: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    quote_volume: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)
    trade_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    taker_buy_volume: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)
    taker_buy_quote_volume: Mapped[Decimal] = mapped_column(sa.Numeric(30, 8), nullable=False)

    def __repr__(self) -> str:
        return f"<Kline {self.symbol} {self.timeframe} {self.open_time} close={self.close}>"


class FundingRate(Base):
    __tablename__ = "funding_rates"
    __table_args__ = (
        sa.UniqueConstraint("symbol", "funding_time", name="uq_funding_rates"),
        sa.Index("ix_funding_symbol_time", "symbol", "funding_time"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    funding_time: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(sa.Numeric(20, 10), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(sa.Numeric(20, 8), nullable=False)

    def __repr__(self) -> str:
        return f"<FundingRate {self.symbol} {self.funding_time} rate={self.funding_rate}>"


class OrderBookSnapshot(Base):
    __tablename__ = "order_book_snapshots"
    __table_args__ = (
        sa.Index("ix_ob_symbol_time", "symbol", "timestamp"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    bids: Mapped[list] = mapped_column(JSONB, nullable=False)   # [[price, qty], ...]
    asks: Mapped[list] = mapped_column(JSONB, nullable=False)
    last_update_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)

    def __repr__(self) -> str:
        return f"<OrderBookSnapshot {self.symbol} {self.timestamp}>"
