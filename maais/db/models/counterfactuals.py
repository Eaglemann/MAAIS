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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue

MONEY = Numeric(38, 18)


class CounterfactualModel(Base):
    __tablename__ = "counterfactuals"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_counterfactual_proposal"),
        CheckConstraint("direction IN ('long', 'short')", name="ck_counterfactual_direction"),
        CheckConstraint(
            "status IN ('pending', 'no_fill', 'open', 'resolved')",
            name="ck_counterfactual_status",
        ),
        CheckConstraint("quantity > 0", name="ck_counterfactual_quantity_positive"),
        CheckConstraint(
            "decision_executable_price > 0",
            name="ck_counterfactual_decision_price_positive",
        ),
        CheckConstraint("fee_rate >= 0 AND fee_rate < 1", name="ck_counterfactual_fee_rate"),
        CheckConstraint(
            "maximum_favorable_excursion >= 0 AND maximum_adverse_excursion >= 0",
            name="ck_counterfactual_excursions",
        ),
        CheckConstraint("version > 0", name="ck_counterfactual_version_positive"),
        CheckConstraint(
            "(status IN ('no_fill', 'resolved') AND closed_at IS NOT NULL) OR "
            "(status IN ('pending', 'open') AND closed_at IS NULL)",
            name="ck_counterfactual_terminal_time",
        ),
        CheckConstraint(
            "(status IN ('pending', 'no_fill') AND hypothetical_fill_at IS NULL) OR "
            "(status IN ('open', 'resolved') AND hypothetical_fill_at IS NOT NULL)",
            name="ck_counterfactual_fill_time",
        ),
        Index("ix_counterfactuals_experiment_status", "experiment_id", "status", "created_at"),
        Index("ix_counterfactuals_gate_status", "rejection_gate", "status", "created_at"),
        Index(
            "ix_counterfactuals_research_tracking",
            "experiment_id",
            "symbol",
            "hypothetical_fill_at",
            postgresql_where=text("status = 'resolved' AND outcome_24h IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("trade_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    decision_cycle_id: Mapped[UUID] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_gate: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_gate_chain_json: Mapped[list[MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    decision_executable_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    eligible_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fee_rate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expected_loss_fraction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expected_gain_fraction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    hypothetical_fill_json: Mapped[dict[str, MutableJsonValue] | None] = mapped_column(JSONB)
    hypothetical_fill_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_policy_json: Mapped[dict[str, MutableJsonValue] | None] = mapped_column(JSONB)
    maximum_favorable_excursion: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    maximum_adverse_excursion: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    outcome_15m: Mapped[Decimal | None] = mapped_column(MONEY)
    outcome_1h: Mapped[Decimal | None] = mapped_column(MONEY)
    outcome_4h: Mapped[Decimal | None] = mapped_column(MONEY)
    outcome_24h: Mapped[Decimal | None] = mapped_column(MONEY)
    funding: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    no_fill_reason: Mapped[str | None] = mapped_column(String(128))
    hypothetical_exit_reason: Mapped[str | None] = mapped_column(String(64))
    hypothetical_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
