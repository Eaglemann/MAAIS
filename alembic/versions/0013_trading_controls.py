"""Add persistent event-backed trading controls.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "trading_controls",
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("kill_switch_active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(1000)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.String(128), nullable=False),
        sa.Column("state_json", jsonb, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_trading_control_version_positive"),
        sa.CheckConstraint("changed_by <> ''", name="ck_trading_control_actor"),
        sa.CheckConstraint(
            "(kill_switch_active AND reason IS NOT NULL AND reason <> '') OR "
            "(NOT kill_switch_active AND reason IS NULL)",
            name="ck_trading_control_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(state_json) = 'object'",
            name="ck_trading_control_state_object",
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64",
            name="ck_trading_control_content_hash",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("experiment_id"),
    )
    op.create_index(
        "ix_trading_controls_active_time",
        "trading_controls",
        ["kill_switch_active", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trading_controls_active_time", table_name="trading_controls")
    op.drop_table("trading_controls")
