"""Index counterfactuals that still need fixed-horizon research observations.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "counterfactuals",
        sa.Column("hypothetical_fill_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE counterfactuals
        SET hypothetical_fill_at = (hypothetical_fill_json ->> 'fill_at')::timestamptz
        WHERE hypothetical_fill_json IS NOT NULL
        """
    )
    op.create_check_constraint(
        "ck_counterfactual_fill_time",
        "counterfactuals",
        "(status IN ('pending', 'no_fill') AND hypothetical_fill_at IS NULL) OR "
        "(status IN ('open', 'resolved') AND hypothetical_fill_at IS NOT NULL)",
    )
    op.create_index(
        "ix_counterfactuals_research_tracking",
        "counterfactuals",
        ["experiment_id", "symbol", "hypothetical_fill_at"],
        unique=False,
        postgresql_where=sa.text("status = 'resolved' AND outcome_24h IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_counterfactuals_research_tracking", table_name="counterfactuals")
    op.drop_constraint(
        "ck_counterfactual_fill_time",
        "counterfactuals",
        type_="check",
    )
    op.drop_column("counterfactuals", "hypothetical_fill_at")
