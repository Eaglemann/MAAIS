"""Mechanically isolated rejected-proposal counterfactual projections.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    money = sa.Numeric(38, 18)
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "counterfactuals",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("proposal_id", uuid, nullable=False),
        sa.Column("decision_cycle_id", uuid, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("rejection_gate", sa.String(64), nullable=False),
        sa.Column("prior_gate_chain_json", jsonb, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("quantity", money, nullable=False),
        sa.Column("eligible_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fee_rate", money, nullable=False),
        sa.Column("expected_loss_fraction", money, nullable=False),
        sa.Column("expected_gain_fraction", money, nullable=False),
        sa.Column("hypothetical_fill_json", jsonb),
        sa.Column("exit_policy_json", jsonb),
        sa.Column("maximum_favorable_excursion", money, nullable=False),
        sa.Column("maximum_adverse_excursion", money, nullable=False),
        sa.Column("outcome_15m", money),
        sa.Column("outcome_1h", money),
        sa.Column("outcome_4h", money),
        sa.Column("outcome_24h", money),
        sa.Column("funding", money, nullable=False),
        sa.Column("no_fill_reason", sa.String(128)),
        sa.Column("hypothetical_exit_reason", sa.String(64)),
        sa.Column("hypothetical_pnl", money),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("state_json", jsonb, nullable=False),
        sa.CheckConstraint("direction IN ('long', 'short')", name="ck_counterfactual_direction"),
        sa.CheckConstraint(
            "status IN ('pending', 'no_fill', 'open', 'resolved')",
            name="ck_counterfactual_status",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_counterfactual_quantity_positive"),
        sa.CheckConstraint("fee_rate >= 0 AND fee_rate < 1", name="ck_counterfactual_fee_rate"),
        sa.CheckConstraint(
            "maximum_favorable_excursion >= 0 AND maximum_adverse_excursion >= 0",
            name="ck_counterfactual_excursions",
        ),
        sa.CheckConstraint("version > 0", name="ck_counterfactual_version_positive"),
        sa.CheckConstraint(
            "(status IN ('no_fill', 'resolved') AND closed_at IS NOT NULL) OR "
            "(status IN ('pending', 'open') AND closed_at IS NULL)",
            name="ck_counterfactual_terminal_time",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["proposal_id"], ["trade_proposals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decision_cycle_id"], ["decision_cycles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", name="uq_counterfactual_proposal"),
    )
    op.create_index(
        "ix_counterfactuals_experiment_status",
        "counterfactuals",
        ["experiment_id", "status", "created_at"],
    )
    op.create_index(
        "ix_counterfactuals_gate_status",
        "counterfactuals",
        ["rejection_gate", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_counterfactuals_gate_status", table_name="counterfactuals")
    op.drop_index("ix_counterfactuals_experiment_status", table_name="counterfactuals")
    op.drop_table("counterfactuals")
