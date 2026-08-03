"""Persist restartable protective-exit trigger state.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    money = sa.Numeric(38, 18)
    op.add_column("exit_plans", sa.Column("trigger_reason", sa.String(32)))
    op.add_column(
        "exit_plans",
        sa.Column("triggered_at", sa.DateTime(timezone=True)),
    )
    op.add_column("exit_plans", sa.Column("trigger_price", money))
    op.add_column("exit_plans", sa.Column("trigger_executable_price", money))
    op.execute(
        "UPDATE exit_plans SET "
        "trigger_reason = 'legacy_unknown', triggered_at = changed_at, "
        "trigger_executable_price = average_entry "
        "WHERE status IN ('triggered', 'closed')"
    )
    op.create_check_constraint(
        "ck_exit_trigger_state",
        "exit_plans",
        "(status = 'active' AND trigger_reason IS NULL AND triggered_at IS NULL "
        "AND trigger_price IS NULL AND trigger_executable_price IS NULL) OR "
        "(status IN ('triggered', 'closed') AND trigger_reason IS NOT NULL "
        "AND triggered_at IS NOT NULL AND trigger_executable_price > 0) OR "
        "status = 'superseded'",
    )
    op.drop_index("uq_exit_one_active_position", table_name="exit_plans")
    op.create_index(
        "uq_exit_one_active_position",
        "exit_plans",
        ["position_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'triggered')"),
    )
    op.add_column(
        "funding_entries",
        sa.Column("funding_at", sa.DateTime(timezone=True)),
    )
    op.add_column("funding_entries", sa.Column("rate_type", sa.String(16)))
    op.execute("UPDATE funding_entries SET funding_at = observed_at, rate_type = 'Regular'")
    op.alter_column("funding_entries", "funding_at", nullable=False)
    op.alter_column("funding_entries", "rate_type", nullable=False)
    op.create_check_constraint(
        "ck_funding_observation_order",
        "funding_entries",
        "funding_at <= observed_at",
    )
    op.create_check_constraint(
        "ck_funding_rate_type",
        "funding_entries",
        "rate_type IN ('Regular', 'Special')",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE funding_entries DROP CONSTRAINT IF EXISTS ck_funding_rate_type")
    op.execute("ALTER TABLE funding_entries DROP CONSTRAINT IF EXISTS ck_funding_observation_order")
    op.execute("ALTER TABLE funding_entries DROP COLUMN IF EXISTS rate_type")
    op.execute("ALTER TABLE funding_entries DROP COLUMN IF EXISTS funding_at")
    op.drop_index("uq_exit_one_active_position", table_name="exit_plans")
    op.create_index(
        "uq_exit_one_active_position",
        "exit_plans",
        ["position_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_constraint("ck_exit_trigger_state", "exit_plans", type_="check")
    op.drop_column("exit_plans", "trigger_executable_price")
    op.drop_column("exit_plans", "trigger_price")
    op.drop_column("exit_plans", "triggered_at")
    op.drop_column("exit_plans", "trigger_reason")
