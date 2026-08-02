"""Persist complete causal market-frame evidence.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    money = sa.Numeric(38, 18)
    empty_json = sa.text("'{}'::jsonb")
    op.add_column("market_frames", sa.Column("index_price", money))
    op.add_column("market_frames", sa.Column("primary_spot_price", money))
    op.add_column("market_frames", sa.Column("secondary_venue_price", money))
    op.add_column(
        "market_frames",
        sa.Column(
            "bar_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_json,
        ),
    )
    op.add_column(
        "market_frames",
        sa.Column(
            "source_manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=empty_json,
        ),
    )
    op.create_check_constraint(
        "ck_frame_reference_prices_positive",
        "market_frames",
        "(index_price IS NULL OR index_price > 0) AND "
        "(primary_spot_price IS NULL OR primary_spot_price > 0) AND "
        "(secondary_venue_price IS NULL OR secondary_venue_price > 0)",
    )
    op.alter_column("market_frames", "bar_snapshot_json", server_default=None)
    op.alter_column("market_frames", "source_manifest_json", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        "ck_frame_reference_prices_positive",
        "market_frames",
        type_="check",
    )
    op.drop_column("market_frames", "source_manifest_json")
    op.drop_column("market_frames", "bar_snapshot_json")
    op.drop_column("market_frames", "secondary_venue_price")
    op.drop_column("market_frames", "primary_spot_price")
    op.drop_column("market_frames", "index_price")
