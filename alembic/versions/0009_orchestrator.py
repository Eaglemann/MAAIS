"""Persistent orchestrator recovery and operational state.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "market_cursors",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("stream", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("event_id", sa.String(256), nullable=False),
        sa.Column("source_sequence", sa.BigInteger(), nullable=False),
        sa.Column("venue_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("state_json", jsonb, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_sequence >= 0", name="ck_market_cursor_sequence_nonnegative"),
        sa.CheckConstraint("version > 0", name="ck_market_cursor_version_positive"),
        sa.CheckConstraint(
            "venue <> '' AND stream <> '' AND symbol <> '' AND timeframe <> '' "
            "AND event_id <> '' AND symbol = upper(symbol)",
            name="ck_market_cursor_identity_fields",
        ),
        sa.CheckConstraint("char_length(content_hash) = 64", name="ck_market_cursor_content_hash"),
        sa.CheckConstraint(
            "jsonb_typeof(state_json) = 'object'", name="ck_market_cursor_state_object"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'recovering', 'halted')",
            name="ck_market_cursor_status",
        ),
        sa.CheckConstraint(
            "venue_event_at <= observed_at AND bar_close_at <= observed_at",
            name="ck_market_cursor_observation_order",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "venue",
            "stream",
            "symbol",
            "timeframe",
            name="uq_market_cursor_identity",
        ),
    )
    op.create_index(
        "ix_market_cursors_experiment_status",
        "market_cursors",
        ["experiment_id", "status", "updated_at"],
    )

    op.create_table(
        "data_quality_evaluations",
        sa.Column("id", uuid, nullable=False),
        sa.Column("market_frame_id", uuid, nullable=False),
        sa.Column("check_name", sa.String(64), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("details_json", jsonb, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'not_applicable')",
            name="ck_quality_evaluation_status",
        ),
        sa.CheckConstraint(
            "check_name <> '' AND reason_code <> ''", name="ck_quality_evaluation_identity"
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64", name="ck_quality_evaluation_content_hash"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details_json) = 'object'", name="ck_quality_evaluation_details_object"
        ),
        sa.ForeignKeyConstraint(["market_frame_id"], ["market_frames.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_frame_id", "check_name", name="uq_quality_frame_check"),
    )
    op.create_index(
        "ix_quality_evaluations_status_time",
        "data_quality_evaluations",
        ["status", "evaluated_at"],
    )

    op.create_table(
        "market_recovery_runs",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("stream", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("gap_start_sequence", sa.BigInteger(), nullable=False),
        sa.Column("gap_end_sequence_exclusive", sa.BigInteger(), nullable=False),
        sa.Column("gap_start_open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_end_open_at_exclusive", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64)),
        sa.Column("failure_reason", sa.String(1000)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_json", jsonb, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "status IN ('detected', 'backfilling', 'completed', 'failed')",
            name="ck_market_recovery_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_market_recovery_attempt_nonnegative"),
        sa.CheckConstraint("interval_seconds > 0", name="ck_market_recovery_interval_positive"),
        sa.CheckConstraint(
            "gap_start_sequence >= 0 AND gap_end_sequence_exclusive > gap_start_sequence",
            name="ck_market_recovery_sequence_range",
        ),
        sa.CheckConstraint("version > 0", name="ck_market_recovery_version_positive"),
        sa.CheckConstraint(
            "venue <> '' AND stream <> '' AND symbol <> '' AND timeframe <> '' "
            "AND symbol = upper(symbol)",
            name="ck_market_recovery_identity_fields",
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64 AND "
            "(source_hash IS NULL OR char_length(source_hash) = 64)",
            name="ck_market_recovery_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(state_json) = 'object'", name="ck_market_recovery_state_object"
        ),
        sa.CheckConstraint(
            "gap_start_open_at < gap_end_open_at_exclusive",
            name="ck_market_recovery_gap_order",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND source_hash IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL)",
            name="ck_market_recovery_completion",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_market_recovery_active",
        "market_recovery_runs",
        ["experiment_id", "venue", "stream", "symbol", "timeframe"],
        unique=True,
        postgresql_where=sa.text("status IN ('detected', 'backfilling')"),
    )
    op.create_index(
        "ix_market_recovery_experiment_status",
        "market_recovery_runs",
        ["experiment_id", "status", "changed_at"],
    )

    op.create_table(
        "incidents",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("deduplication_key", sa.String(256), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("component", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("evidence_json", jsonb, nullable=False),
        sa.Column("requires_operator_review", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.String(128)),
        sa.Column("resolved_by", sa.String(128)),
        sa.Column("resolution", sa.String(1000)),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state_json", jsonb, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "severity IN ('warning', 'error', 'critical')",
            name="ck_incident_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_incident_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_incident_version_positive"),
        sa.CheckConstraint(
            "deduplication_key <> '' AND component <> '' AND reason_code <> ''",
            name="ck_incident_identity_fields",
        ),
        sa.CheckConstraint("char_length(content_hash) = 64", name="ck_incident_content_hash"),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object' AND jsonb_typeof(state_json) = 'object'",
            name="ck_incident_json_objects",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND acknowledged_at IS NULL AND resolved_at IS NULL) OR "
            "(status = 'acknowledged' AND acknowledged_at IS NOT NULL AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_incident_transition_times",
        ),
        sa.CheckConstraint(
            "(acknowledged_at IS NULL OR detected_at <= acknowledged_at) AND "
            "(resolved_at IS NULL OR detected_at <= resolved_at)",
            name="ck_incident_time_order",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "deduplication_key", name="uq_incident_deduplication"),
    )
    op.create_index(
        "ix_incidents_experiment_status",
        "incidents",
        ["experiment_id", "status", "detected_at"],
    )
    op.create_index(
        "ix_incidents_component_status",
        "incidents",
        ["component", "status", "detected_at"],
    )

    op.create_table(
        "worker_checkpoints",
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("worker_id", uuid, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("state_json", jsonb, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("checkpoint_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_worker_checkpoint_version_positive"),
        sa.CheckConstraint(
            "char_length(content_hash) = 64", name="ck_worker_checkpoint_content_hash"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(state_json) = 'object'", name="ck_worker_checkpoint_state_object"
        ),
        sa.CheckConstraint(
            "status IN ('starting', 'running', 'recovering', 'stopping', 'stopped', 'halted')",
            name="ck_worker_checkpoint_status",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("experiment_id"),
    )
    op.create_index(
        "ix_worker_checkpoints_status_time",
        "worker_checkpoints",
        ["status", "checkpoint_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_checkpoints_status_time", table_name="worker_checkpoints")
    op.drop_table("worker_checkpoints")
    op.drop_index("ix_incidents_component_status", table_name="incidents")
    op.drop_index("ix_incidents_experiment_status", table_name="incidents")
    op.drop_table("incidents")
    op.drop_index("ix_market_recovery_experiment_status", table_name="market_recovery_runs")
    op.drop_index("uq_market_recovery_active", table_name="market_recovery_runs")
    op.drop_table("market_recovery_runs")
    op.drop_index("ix_quality_evaluations_status_time", table_name="data_quality_evaluations")
    op.drop_table("data_quality_evaluations")
    op.drop_index("ix_market_cursors_experiment_status", table_name="market_cursors")
    op.drop_table("market_cursors")
