"""Event ledger and immutable experiment identity.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_streams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("current_version >= 0", name="ck_event_stream_version_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("aggregate_type", "aggregate_id", name="uq_event_stream_aggregate"),
    )
    op.create_table(
        "experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("initial_capital", sa.Numeric(38, 18), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("git_sha", sa.String(64), nullable=False),
        sa.Column("worktree_hash", sa.String(64)),
        sa.Column("lock_hash", sa.String(64), nullable=False),
        sa.Column("schema_revision", sa.String(32), nullable=False),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("manifest_schema_version", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(1000)),
        sa.CheckConstraint("initial_capital > 0", name="ck_experiment_initial_capital_positive"),
        sa.CheckConstraint(
            "manifest_schema_version > 0", name="ck_experiment_manifest_version_positive"
        ),
        sa.CheckConstraint(
            "mode IN ('replay', 'paper_live', 'testnet_smoke')", name="ck_experiment_mode"
        ),
        sa.CheckConstraint(
            "status IN ('created', 'running', 'paused', 'stopped', 'completed', 'failed')",
            name="ck_experiment_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manifest_hash", name="uq_experiment_manifest_hash"),
    )
    op.create_index("ix_experiments_config_hash", "experiments", ["config_hash"])
    op.create_index("ix_experiments_mode_created", "experiments", ["mode", "created_at"])
    op.create_index("ix_experiments_status_created", "experiments", ["status", "created_at"])
    op.create_table(
        "strategy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_key", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("implementation_hash", sa.String(64), nullable=False),
        sa.Column("parameter_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "stage IN ('research', 'simulation', 'pilot', 'full_production')",
            name="ck_strategy_version_stage",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_key", "version", name="uq_strategy_version_identity"),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("maturity", sa.String(32), nullable=False),
        sa.Column("implementation_hash", sa.String(64), nullable=False),
        sa.Column("parameter_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "data_dependencies_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "maturity IN ('implemented', 'proxy', 'disabled')",
            name="ck_agent_version_maturity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_name", "version", name="uq_agent_version_identity"),
    )
    op.create_table(
        "domain_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("global_position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("stream_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("event_version > 0", name="ck_domain_event_version_positive"),
        sa.CheckConstraint("stream_version > 0", name="ck_domain_event_stream_version_positive"),
        sa.ForeignKeyConstraint(["stream_id"], ["event_streams.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("global_position", name="uq_domain_event_global_position"),
        sa.UniqueConstraint("stream_id", "stream_version", name="uq_domain_event_stream_version"),
    )
    op.create_index(
        "ix_domain_events_aggregate_time",
        "domain_events",
        ["aggregate_type", "aggregate_id", "occurred_at"],
    )
    op.create_index("ix_domain_events_type_time", "domain_events", ["event_type", "occurred_at"])
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cursor", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("domain_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(1000)),
        sa.CheckConstraint("publish_attempts >= 0", name="ck_outbox_publish_attempts_nonnegative"),
        sa.ForeignKeyConstraint(["domain_event_id"], ["domain_events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cursor", name="uq_outbox_cursor"),
        sa.UniqueConstraint("domain_event_id", name="uq_outbox_domain_event"),
    )
    op.create_index("ix_outbox_unpublished_cursor", "outbox_events", ["published_at", "cursor"])
    op.execute("""
        CREATE FUNCTION maais_prevent_event_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'domain_events is append-only';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_domain_events_append_only
        BEFORE UPDATE OR DELETE ON domain_events
        FOR EACH ROW EXECUTE FUNCTION maais_prevent_event_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_domain_events_append_only ON domain_events")
    op.execute("DROP FUNCTION IF EXISTS maais_prevent_event_mutation()")
    op.drop_index("ix_outbox_unpublished_cursor", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("ix_domain_events_type_time", table_name="domain_events")
    op.drop_index("ix_domain_events_aggregate_time", table_name="domain_events")
    op.drop_table("domain_events")
    op.drop_table("agent_versions")
    op.drop_table("strategy_versions")
    op.drop_index("ix_experiments_status_created", table_name="experiments")
    op.drop_index("ix_experiments_mode_created", table_name="experiments")
    op.drop_index("ix_experiments_config_hash", table_name="experiments")
    op.drop_table("experiments")
    op.drop_table("event_streams")
