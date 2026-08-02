from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue


class MarketCursorModel(Base):
    __tablename__ = "market_cursors"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "venue",
            "stream",
            "symbol",
            "timeframe",
            name="uq_market_cursor_identity",
        ),
        CheckConstraint("source_sequence >= 0", name="ck_market_cursor_sequence_nonnegative"),
        CheckConstraint("version > 0", name="ck_market_cursor_version_positive"),
        CheckConstraint(
            "venue <> '' AND stream <> '' AND symbol <> '' AND timeframe <> '' "
            "AND event_id <> '' AND symbol = upper(symbol)",
            name="ck_market_cursor_identity_fields",
        ),
        CheckConstraint("char_length(content_hash) = 64", name="ck_market_cursor_content_hash"),
        CheckConstraint(
            "jsonb_typeof(state_json) = 'object'", name="ck_market_cursor_state_object"
        ),
        CheckConstraint(
            "status IN ('active', 'recovering', 'halted')",
            name="ck_market_cursor_status",
        ),
        CheckConstraint(
            "venue_event_at <= observed_at AND bar_close_at <= observed_at",
            name="ck_market_cursor_observation_order",
        ),
        Index("ix_market_cursors_experiment_status", "experiment_id", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    stream: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    venue_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_close_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataQualityEvaluationModel(Base):
    __tablename__ = "data_quality_evaluations"
    __table_args__ = (
        UniqueConstraint("market_frame_id", "check_name", name="uq_quality_frame_check"),
        CheckConstraint(
            "status IN ('passed', 'failed', 'not_applicable')",
            name="ck_quality_evaluation_status",
        ),
        CheckConstraint(
            "check_name <> '' AND reason_code <> ''", name="ck_quality_evaluation_identity"
        ),
        CheckConstraint(
            "char_length(content_hash) = 64", name="ck_quality_evaluation_content_hash"
        ),
        CheckConstraint(
            "jsonb_typeof(details_json) = 'object'", name="ck_quality_evaluation_details_object"
        ),
        Index("ix_quality_evaluations_status_time", "status", "evaluated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    market_frame_id: Mapped[UUID] = mapped_column(
        ForeignKey("market_frames.id", ondelete="RESTRICT"), nullable=False
    )
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    details_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class MarketRecoveryRunModel(Base):
    __tablename__ = "market_recovery_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('detected', 'backfilling', 'completed', 'failed')",
            name="ck_market_recovery_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_market_recovery_attempt_nonnegative"),
        CheckConstraint("interval_seconds > 0", name="ck_market_recovery_interval_positive"),
        CheckConstraint(
            "gap_start_sequence >= 0 AND gap_end_sequence_exclusive > gap_start_sequence",
            name="ck_market_recovery_sequence_range",
        ),
        CheckConstraint("version > 0", name="ck_market_recovery_version_positive"),
        CheckConstraint(
            "venue <> '' AND stream <> '' AND symbol <> '' AND timeframe <> '' "
            "AND symbol = upper(symbol)",
            name="ck_market_recovery_identity_fields",
        ),
        CheckConstraint(
            "char_length(content_hash) = 64 AND "
            "(source_hash IS NULL OR char_length(source_hash) = 64)",
            name="ck_market_recovery_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(state_json) = 'object'", name="ck_market_recovery_state_object"
        ),
        CheckConstraint(
            "gap_start_open_at < gap_end_open_at_exclusive",
            name="ck_market_recovery_gap_order",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND source_hash IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL)",
            name="ck_market_recovery_completion",
        ),
        Index(
            "uq_market_recovery_active",
            "experiment_id",
            "venue",
            "stream",
            "symbol",
            "timeframe",
            unique=True,
            postgresql_where=text("status IN ('detected', 'backfilling')"),
        ),
        Index(
            "ix_market_recovery_experiment_status",
            "experiment_id",
            "status",
            "changed_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    stream: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    gap_start_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gap_end_sequence_exclusive: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gap_start_open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gap_end_open_at_exclusive: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    source_hash: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class IncidentModel(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("experiment_id", "deduplication_key", name="uq_incident_deduplication"),
        CheckConstraint(
            "severity IN ('warning', 'error', 'critical')",
            name="ck_incident_severity",
        ),
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_incident_status",
        ),
        CheckConstraint("version > 0", name="ck_incident_version_positive"),
        CheckConstraint(
            "deduplication_key <> '' AND component <> '' AND reason_code <> ''",
            name="ck_incident_identity_fields",
        ),
        CheckConstraint("char_length(content_hash) = 64", name="ck_incident_content_hash"),
        CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object' AND jsonb_typeof(state_json) = 'object'",
            name="ck_incident_json_objects",
        ),
        CheckConstraint(
            "(status = 'open' AND acknowledged_at IS NULL AND resolved_at IS NULL) OR "
            "(status = 'acknowledged' AND acknowledged_at IS NOT NULL AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_incident_transition_times",
        ),
        CheckConstraint(
            "(acknowledged_at IS NULL OR detected_at <= acknowledged_at) AND "
            "(resolved_at IS NULL OR detected_at <= resolved_at)",
            name="ck_incident_time_order",
        ),
        Index("ix_incidents_experiment_status", "experiment_id", "status", "detected_at"),
        Index("ix_incidents_component_status", "component", "status", "detected_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), nullable=False
    )
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    component: Mapped[str] = mapped_column(String(128), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    requires_operator_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolution: Mapped[str | None] = mapped_column(String(1000))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class WorkerCheckpointModel(Base):
    __tablename__ = "worker_checkpoints"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_worker_checkpoint_version_positive"),
        CheckConstraint("char_length(content_hash) = 64", name="ck_worker_checkpoint_content_hash"),
        CheckConstraint(
            "jsonb_typeof(state_json) = 'object'", name="ck_worker_checkpoint_state_object"
        ),
        CheckConstraint(
            "status IN ('starting', 'running', 'recovering', 'stopping', 'stopped', 'halted')",
            name="ck_worker_checkpoint_status",
        ),
        Index("ix_worker_checkpoints_status_time", "status", "checkpoint_at"),
    )

    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="RESTRICT"), primary_key=True
    )
    worker_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    state_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
