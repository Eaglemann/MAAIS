from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base

MONEY = Numeric(38, 18)


class PositionModel(Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_position_quantity_nonnegative"),
        CheckConstraint("leverage BETWEEN 1 AND 5", name="ck_position_leverage"),
        CheckConstraint("side IN ('long', 'short', 'neutral')", name="ck_position_side"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_position_status"),
        CheckConstraint(
            "(status = 'open' AND quantity > 0 AND side <> 'neutral' AND closed_at IS NULL) OR "
            "(status = 'closed' AND quantity = 0 AND side = 'neutral' AND closed_at IS NOT NULL)",
            name="ck_position_open_closed_state",
        ),
        Index(
            "uq_position_one_open_symbol",
            "experiment_id",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    average_entry: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    initial_margin: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maintenance_margin: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    funding: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class PositionLotModel(Base):
    __tablename__ = "position_lots"
    __table_args__ = (
        CheckConstraint(
            "original_quantity > 0 AND remaining_quantity >= 0 AND "
            "remaining_quantity <= original_quantity",
            name="ck_lot_quantities",
        ),
        CheckConstraint(
            "opening_fee >= 0 AND remaining_opening_fee >= 0 AND "
            "remaining_opening_fee <= opening_fee",
            name="ck_lot_fees",
        ),
        UniqueConstraint("opening_fill_id", name="uq_lot_opening_fill"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    position_id: Mapped[UUID] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"), nullable=False
    )
    opening_fill_id: Mapped[UUID] = mapped_column(
        ForeignKey("fills.id", ondelete="RESTRICT"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    original_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    remaining_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opening_fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    remaining_opening_fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    funding: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class ExitPlanModel(Base):
    __tablename__ = "exit_plans"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_exit_quantity_positive"),
        CheckConstraint("maximum_bars > 0", name="ck_exit_maximum_bars"),
        CheckConstraint("bars_elapsed >= 0", name="ck_exit_bars_elapsed"),
        CheckConstraint("opposite_signal_streak >= 0", name="ck_exit_opposite_streak"),
        CheckConstraint(
            "status IN ('active', 'triggered', 'superseded', 'closed')",
            name="ck_exit_status",
        ),
        Index(
            "uq_exit_one_active_position",
            "position_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'triggered')"),
        ),
        CheckConstraint(
            "(status = 'active' AND trigger_reason IS NULL AND triggered_at IS NULL "
            "AND trigger_price IS NULL AND trigger_executable_price IS NULL) OR "
            "(status IN ('triggered', 'closed') AND trigger_reason IS NOT NULL "
            "AND triggered_at IS NOT NULL AND trigger_executable_price > 0) OR "
            "status = 'superseded'",
            name="ck_exit_trigger_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    position_id: Mapped[UUID] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    average_entry: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expected_loss_fraction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expected_gain_fraction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    target_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_bars: Mapped[int] = mapped_column(Integer, nullable=False)
    bars_elapsed: Mapped[int] = mapped_column(Integer, nullable=False)
    opposite_signal_streak: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger_reason: Mapped[str | None] = mapped_column(String(32))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_price: Mapped[Decimal | None] = mapped_column(MONEY)
    trigger_executable_price: Mapped[Decimal | None] = mapped_column(MONEY)


class AccountSnapshotModel(Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (
        UniqueConstraint("experiment_id", "account_version", name="uq_account_snapshot_version"),
        Index("ix_account_snapshots_experiment_time", "experiment_id", "snapshot_at"),
        CheckConstraint("account_version >= 0", name="ck_account_version_nonnegative"),
        CheckConstraint("drawdown >= 0", name="ck_account_drawdown_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    used_margin: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    free_margin: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gross_notional: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    risk_at_stop: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fees: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    funding: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    peak_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    drawdown: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class FundingEntryModel(Base):
    __tablename__ = "funding_entries"
    __table_args__ = (
        UniqueConstraint("experiment_id", "market_event_id", name="uq_funding_market_event"),
        CheckConstraint("funding_at <= observed_at", name="ck_funding_observation_order"),
        CheckConstraint(
            "rate_type IN ('Regular', 'Special')",
            name="ck_funding_rate_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    position_id: Mapped[UUID] = mapped_column(
        ForeignKey("positions.id", ondelete="RESTRICT"), nullable=False
    )
    funding_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    rate_type: Mapped[str] = mapped_column(String(16), nullable=False)
    notional: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    market_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
