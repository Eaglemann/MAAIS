from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue

MONEY = Numeric(38, 18)


class OrderIntentModel(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        UniqueConstraint("experiment_id", "client_order_id", name="uq_order_client_identity"),
        CheckConstraint("quantity > 0", name="ck_order_quantity_positive"),
        CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_order_filled_quantity",
        ),
        CheckConstraint("created_at < expires_at", name="ck_order_time_order"),
        CheckConstraint("version > 0", name="ck_order_version_positive"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_order_side"),
        CheckConstraint("order_type IN ('market', 'limit', 'stop_market')", name="ck_order_type"),
        CheckConstraint("position_effect IN ('open', 'reduce')", name="ck_order_position_effect"),
        CheckConstraint(
            "status IN ('created', 'authorized', 'accepted', 'partially_filled', "
            "'filled', 'canceled', 'rejected', 'expired')",
            name="ck_order_status",
        ),
        CheckConstraint(
            "(order_type = 'limit' AND limit_price IS NOT NULL) OR "
            "(order_type <> 'limit' AND limit_price IS NULL)",
            name="ck_order_limit_price",
        ),
        CheckConstraint(
            "(position_effect = 'reduce' AND reduce_only) OR "
            "(position_effect = 'open' AND NOT reduce_only)",
            name="ck_order_reduce_only",
        ),
        Index("ix_order_intents_experiment_status", "experiment_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("trade_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    position_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_filter_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class OrderEventModel(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("order_intent_id", "sequence", name="uq_order_event_sequence"),
        CheckConstraint("sequence > 0", name="ck_order_event_sequence_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_frame_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("market_frames.id", ondelete="RESTRICT")
    )
    payload_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)


class FillModel(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("order_intent_id", "market_event_id", name="uq_fill_order_market_event"),
        CheckConstraint("quantity > 0 AND price > 0", name="ck_fill_positive"),
        CheckConstraint("fee >= 0", name="ck_fill_fee_nonnegative"),
        CheckConstraint("liquidity_role IN ('maker', 'taker')", name="ck_fill_liquidity_role"),
        Index("ix_fills_order_time", "order_intent_id", "fill_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.id", ondelete="RESTRICT"), nullable=False
    )
    market_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fill_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    liquidity_role: Mapped[str] = mapped_column(String(16), nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    spread_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    depth_slippage: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    latency_slippage: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_slippage: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    market_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)


class ExecutionSensitivityModel(Base):
    __tablename__ = "execution_sensitivities"
    __table_args__ = (
        UniqueConstraint("order_intent_id", "scenario", name="uq_sensitivity_order_scenario"),
        CheckConstraint(
            "scenario IN ('optimistic', 'conservative', 'stress')",
            name="ck_sensitivity_scenario",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    order_intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("order_intents.id", ondelete="RESTRICT"), nullable=False
    )
    scenario: Mapped[str] = mapped_column(String(16), nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
