"""Persist the exact mark used for every observed funding settlement.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM funding_entries) THEN
                RAISE EXCEPTION
                    'cannot infer exact funding mark_price for existing funding entries';
            END IF;
        END
        $$
        """
    )
    op.add_column(
        "funding_entries",
        sa.Column("mark_price", sa.Numeric(38, 18), nullable=False),
    )
    op.create_check_constraint(
        "ck_funding_mark_price_positive",
        "funding_entries",
        "mark_price > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_funding_mark_price_positive",
        "funding_entries",
        type_="check",
    )
    op.drop_column("funding_entries", "mark_price")
