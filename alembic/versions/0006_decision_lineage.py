"""Complete market and decision lineage projections.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    money = sa.Numeric(38, 18)
    op.create_table(
        "market_frames",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("venue", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("bar_open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", money, nullable=False),
        sa.Column("high", money, nullable=False),
        sa.Column("low", money, nullable=False),
        sa.Column("close", money, nullable=False),
        sa.Column("volume", money, nullable=False),
        sa.Column("best_bid", money),
        sa.Column("best_ask", money),
        sa.Column("mark_price", money),
        sa.Column("funding_rate", money),
        sa.Column(
            "orderbook_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_sequence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quality_status", sa.String(32), nullable=False),
        sa.Column("quality_results_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_frame_prices_positive"
        ),
        sa.CheckConstraint("volume >= 0", name="ck_frame_volume_nonnegative"),
        sa.CheckConstraint(
            "low <= open AND low <= close AND high >= open AND high >= close",
            name="ck_frame_ohlc",
        ),
        sa.CheckConstraint("bar_open_at < bar_close_at", name="ck_frame_bar_time_order"),
        sa.CheckConstraint("bar_close_at <= observed_at", name="ck_frame_observed_time_order"),
        sa.CheckConstraint(
            "quality_status IN ('passed', 'failed', 'not_applicable')",
            name="ck_frame_quality_status",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "content_hash", name="uq_market_frame_content"),
    )
    op.create_index(
        "ix_market_frames_experiment_symbol_close",
        "market_frames",
        ["experiment_id", "symbol", "bar_close_at"],
    )
    op.create_table(
        "decision_cycles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market_frame_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("cycle_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("regime", sa.String(64), nullable=False),
        sa.Column("feature_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("feature_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('completed', 'rejected', 'quarantined')",
            name="ck_decision_cycle_status",
        ),
        sa.CheckConstraint(
            "direction IN ('long', 'short', 'neutral')", name="ck_decision_direction"
        ),
        sa.CheckConstraint(
            "disposition IN ('neutral', 'rejected', 'approved')",
            name="ck_decision_disposition",
        ),
        sa.CheckConstraint("created_at <= completed_at", name="ck_decision_time_order"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["market_frame_id"], ["market_frames.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "symbol",
            "timeframe",
            "cycle_at",
            "strategy_version_id",
            name="uq_decision_cycle_key",
        ),
    )
    op.create_index(
        "ix_decision_cycles_experiment_time", "decision_cycles", ["experiment_id", "cycle_at"]
    )
    op.create_index(
        "ix_decision_cycles_symbol_disposition",
        "decision_cycles",
        ["symbol", "disposition", "cycle_at"],
    )
    op.create_index("ix_decision_cycles_reason", "decision_cycles", ["reason_code", "cycle_at"])
    op.create_table(
        "agent_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("compatible", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("weight", money, nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("probability", money, nullable=False),
        sa.Column("confidence", money, nullable=False),
        sa.Column("risk", money, nullable=False),
        sa.Column("input_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason_codes_json", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("explanation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("weight > 0", name="ck_agent_evaluation_weight_positive"),
        sa.CheckConstraint("probability >= 0 AND probability <= 1", name="ck_agent_probability"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_agent_confidence"),
        sa.CheckConstraint("risk >= 0 AND risk <= 1", name="ck_agent_risk"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_agent_duration_nonnegative"),
        sa.CheckConstraint("direction IN ('long', 'short', 'neutral')", name="ck_agent_direction"),
        sa.ForeignKeyConstraint(["decision_cycle_id"], ["decision_cycles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_cycle_id",
            "agent_version_id",
            name="uq_agent_evaluation_cycle_version",
        ),
    )
    op.create_index("ix_agent_evaluations_cycle", "agent_evaluations", ["decision_cycle_id"])
    op.create_table(
        "decision_summaries",
        sa.Column("decision_cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consensus_direction", sa.String(16), nullable=False),
        sa.Column("consensus_probability", money, nullable=False),
        sa.Column("consensus_confidence", money, nullable=False),
        sa.Column("long_weight", money, nullable=False),
        sa.Column("short_weight", money, nullable=False),
        sa.Column("neutral_weight", money, nullable=False),
        sa.Column("dissenters_json", postgresql.ARRAY(sa.String(128)), nullable=False),
        sa.Column("dissent_probability", money, nullable=False),
        sa.Column("dissent_confidence", money, nullable=False),
        sa.Column("challenge_blocked", sa.Boolean(), nullable=False),
        sa.Column("expected_gain", money, nullable=False),
        sa.Column("expected_loss", money, nullable=False),
        sa.Column("gross_ev", money, nullable=False),
        sa.Column("funding_carry", money, nullable=False),
        sa.Column("estimated_cost", money, nullable=False),
        sa.Column("net_ev", money, nullable=False),
        sa.Column("benchmark_return", money, nullable=False),
        sa.Column("alpha_estimate", money, nullable=False),
        sa.Column(
            "consensus_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "adversarial_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("ev_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cost_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "consensus_direction IN ('long', 'short', 'neutral')",
            name="ck_summary_direction",
        ),
        sa.CheckConstraint(
            "consensus_probability >= 0 AND consensus_probability <= 1",
            name="ck_summary_probability",
        ),
        sa.CheckConstraint(
            "consensus_confidence >= 0 AND consensus_confidence <= 1",
            name="ck_summary_confidence",
        ),
        sa.ForeignKeyConstraint(["decision_cycle_id"], ["decision_cycles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("decision_cycle_id"),
    )
    op.create_table(
        "gate_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gate_type", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("input_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_gate_sequence_positive"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_gate_duration_nonnegative"),
        sa.ForeignKeyConstraint(["decision_cycle_id"], ["decision_cycles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_cycle_id", "sequence", name="uq_gate_evaluation_cycle_sequence"
        ),
        sa.UniqueConstraint("decision_cycle_id", "gate_type", name="uq_gate_evaluation_cycle_type"),
    )
    op.create_index(
        "ix_gate_evaluations_type_passed",
        "gate_evaluations",
        ["gate_type", "passed", "evaluated_at"],
    )
    op.create_table(
        "trade_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_cycle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_policy_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exit_policy_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sizing_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approved_quantity", money),
        sa.Column("approved_notional", money),
        sa.Column("risk_at_stop", money),
        sa.CheckConstraint("direction IN ('long', 'short')", name="ck_proposal_direction"),
        sa.CheckConstraint(
            "status IN ('rejected', 'approved', 'expired')",
            name="ck_proposal_status",
        ),
        sa.CheckConstraint("proposed_at < expires_at", name="ck_proposal_time_order"),
        sa.ForeignKeyConstraint(["decision_cycle_id"], ["decision_cycles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_cycle_id", name="uq_trade_proposal_cycle"),
    )
    op.create_index(
        "ix_trade_proposals_experiment_status",
        "trade_proposals",
        ["experiment_id", "status", "proposed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trade_proposals_experiment_status", table_name="trade_proposals")
    op.drop_table("trade_proposals")
    op.drop_index("ix_gate_evaluations_type_passed", table_name="gate_evaluations")
    op.drop_table("gate_evaluations")
    op.drop_table("decision_summaries")
    op.drop_index("ix_agent_evaluations_cycle", table_name="agent_evaluations")
    op.drop_table("agent_evaluations")
    op.drop_index("ix_decision_cycles_reason", table_name="decision_cycles")
    op.drop_index("ix_decision_cycles_symbol_disposition", table_name="decision_cycles")
    op.drop_index("ix_decision_cycles_experiment_time", table_name="decision_cycles")
    op.drop_table("decision_cycles")
    op.drop_index("ix_market_frames_experiment_symbol_close", table_name="market_frames")
    op.drop_table("market_frames")
