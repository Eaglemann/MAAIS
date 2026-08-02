"""Persist the decision executable price for counterfactual fills.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM counterfactuals) THEN
                RAISE EXCEPTION
                    'cannot infer exact decision price for existing counterfactuals';
            END IF;
        END
        $$
        """
    )
    op.add_column(
        "counterfactuals",
        sa.Column("decision_executable_price", sa.Numeric(38, 18), nullable=False),
    )
    op.create_check_constraint(
        "ck_counterfactual_decision_price_positive",
        "counterfactuals",
        "decision_executable_price > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_counterfactual_decision_price_positive",
        "counterfactuals",
        type_="check",
    )
    op.drop_column("counterfactuals", "decision_executable_price")
