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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue

MONEY = Numeric(38, 18)


class MarketFrameModel(Base):
    __tablename__ = "market_frames"
    __table_args__ = (
        UniqueConstraint("experiment_id", "content_hash", name="uq_market_frame_content"),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_frame_prices_positive"
        ),
        CheckConstraint("volume >= 0", name="ck_frame_volume_nonnegative"),
        CheckConstraint(
            "low <= open AND low <= close AND high >= open AND high >= close", name="ck_frame_ohlc"
        ),
        CheckConstraint(
            "(index_price IS NULL OR index_price > 0) AND "
            "(primary_spot_price IS NULL OR primary_spot_price > 0) AND "
            "(secondary_venue_price IS NULL OR secondary_venue_price > 0)",
            name="ck_frame_reference_prices_positive",
        ),
        CheckConstraint("bar_open_at < bar_close_at", name="ck_frame_bar_time_order"),
        CheckConstraint("bar_close_at <= observed_at", name="ck_frame_observed_time_order"),
        CheckConstraint(
            "quality_status IN ('passed', 'failed', 'not_applicable')",
            name="ck_frame_quality_status",
        ),
        Index(
            "ix_market_frames_experiment_symbol_close", "experiment_id", "symbol", "bar_close_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    bar_open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    high: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    low: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    close: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    volume: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    best_bid: Mapped[Decimal | None] = mapped_column(MONEY)
    best_ask: Mapped[Decimal | None] = mapped_column(MONEY)
    mark_price: Mapped[Decimal | None] = mapped_column(MONEY)
    index_price: Mapped[Decimal | None] = mapped_column(MONEY)
    funding_rate: Mapped[Decimal | None] = mapped_column(MONEY)
    primary_spot_price: Mapped[Decimal | None] = mapped_column(MONEY)
    secondary_venue_price: Mapped[Decimal | None] = mapped_column(MONEY)
    bar_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    orderbook_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB, nullable=False
    )
    source_manifest_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    source_sequence_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_results_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class DecisionCycleModel(Base):
    __tablename__ = "decision_cycles"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "symbol",
            "timeframe",
            "cycle_at",
            "strategy_version_id",
            name="uq_decision_cycle_key",
        ),
        CheckConstraint(
            "status IN ('completed', 'rejected', 'quarantined')",
            name="ck_decision_cycle_status",
        ),
        CheckConstraint("direction IN ('long', 'short', 'neutral')", name="ck_decision_direction"),
        CheckConstraint(
            "disposition IN ('neutral', 'rejected', 'approved')",
            name="ck_decision_disposition",
        ),
        CheckConstraint("created_at <= completed_at", name="ck_decision_time_order"),
        Index("ix_decision_cycles_experiment_time", "experiment_id", "cycle_at"),
        Index("ix_decision_cycles_symbol_disposition", "symbol", "disposition", "cycle_at"),
        Index("ix_decision_cycles_reason", "reason_code", "cycle_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    market_frame_id: Mapped[UUID] = mapped_column(
        ForeignKey("market_frames.id", ondelete="RESTRICT"), nullable=False
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    cycle_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    regime: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB, nullable=False
    )
    feature_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentEvaluationModel(Base):
    __tablename__ = "agent_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "decision_cycle_id", "agent_version_id", name="uq_agent_evaluation_cycle_version"
        ),
        CheckConstraint("weight > 0", name="ck_agent_evaluation_weight_positive"),
        CheckConstraint("probability >= 0 AND probability <= 1", name="ck_agent_probability"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_agent_confidence"),
        CheckConstraint("risk >= 0 AND risk <= 1", name="ck_agent_risk"),
        CheckConstraint("duration_ms >= 0", name="ck_agent_duration_nonnegative"),
        CheckConstraint("direction IN ('long', 'short', 'neutral')", name="ck_agent_direction"),
        Index("ix_agent_evaluations_cycle", "decision_cycle_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    decision_cycle_id: Mapped[UUID] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    compatible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    weight: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    probability: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    risk: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    input_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    explanation_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DecisionSummaryModel(Base):
    __tablename__ = "decision_summaries"
    __table_args__ = (
        CheckConstraint(
            "consensus_direction IN ('long', 'short', 'neutral')",
            name="ck_summary_direction",
        ),
        CheckConstraint(
            "consensus_probability >= 0 AND consensus_probability <= 1",
            name="ck_summary_probability",
        ),
        CheckConstraint(
            "consensus_confidence >= 0 AND consensus_confidence <= 1",
            name="ck_summary_confidence",
        ),
    )

    decision_cycle_id: Mapped[UUID] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="RESTRICT"), primary_key=True
    )
    consensus_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    consensus_probability: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    consensus_confidence: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    long_weight: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    short_weight: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    neutral_weight: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    dissenters_json: Mapped[list[str]] = mapped_column(ARRAY(String(128)), nullable=False)
    dissent_probability: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    dissent_confidence: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    challenge_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_gain: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    expected_loss: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gross_ev: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    funding_carry: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    estimated_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_ev: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    benchmark_return: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    alpha_estimate: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    consensus_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB, nullable=False
    )
    adversarial_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB, nullable=False
    )
    ev_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    cost_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)


class GateEvaluationModel(Base):
    __tablename__ = "gate_evaluations"
    __table_args__ = (
        UniqueConstraint("decision_cycle_id", "sequence", name="uq_gate_evaluation_cycle_sequence"),
        UniqueConstraint("decision_cycle_id", "gate_type", name="uq_gate_evaluation_cycle_type"),
        CheckConstraint("sequence > 0", name="ck_gate_sequence_positive"),
        CheckConstraint("duration_ms >= 0", name="ck_gate_duration_nonnegative"),
        Index("ix_gate_evaluations_type_passed", "gate_type", "passed", "evaluated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    decision_cycle_id: Mapped[UUID] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="RESTRICT"), nullable=False
    )
    gate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    input_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    output_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)


class TradeProposalModel(Base):
    __tablename__ = "trade_proposals"
    __table_args__ = (
        UniqueConstraint("decision_cycle_id", name="uq_trade_proposal_cycle"),
        CheckConstraint("direction IN ('long', 'short')", name="ck_proposal_direction"),
        CheckConstraint(
            "status IN ('rejected', 'approved', 'expired')",
            name="ck_proposal_status",
        ),
        CheckConstraint("proposed_at < expires_at", name="ck_proposal_time_order"),
        Index("ix_trade_proposals_experiment_status", "experiment_id", "status", "proposed_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    decision_cycle_id: Mapped[UUID] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="RESTRICT"), nullable=False
    )
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_policy_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    exit_policy_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    sizing_snapshot_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    approved_quantity: Mapped[Decimal | None] = mapped_column(MONEY)
    approved_notional: Mapped[Decimal | None] = mapped_column(MONEY)
    risk_at_stop: Mapped[Decimal | None] = mapped_column(MONEY)
