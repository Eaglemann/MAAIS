"""Market data tables: klines, funding_rates, order_book_snapshots.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "klines",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(20, 8), nullable=False),
        sa.Column("high", sa.Numeric(20, 8), nullable=False),
        sa.Column("low", sa.Numeric(20, 8), nullable=False),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quote_volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("trade_count", sa.Integer(), nullable=False),
        sa.Column("taker_buy_volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("taker_buy_quote_volume", sa.Numeric(30, 8), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "timeframe", "open_time", name="uq_klines"),
    )
    op.create_index("ix_klines_symbol_tf_time", "klines", ["symbol", "timeframe", "open_time"])
    op.create_index("ix_klines_symbol_time", "klines", ["symbol", "open_time"])

    op.create_table(
        "funding_rates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", sa.Numeric(20, 10), nullable=False),
        sa.Column("mark_price", sa.Numeric(20, 8), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "funding_time", name="uq_funding_rates"),
    )
    op.create_index("ix_funding_symbol_time", "funding_rates", ["symbol", "funding_time"])

    op.create_table(
        "order_book_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bids", postgresql.JSONB(), nullable=False),
        sa.Column("asks", postgresql.JSONB(), nullable=False),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ob_symbol_time", "order_book_snapshots", ["symbol", "timestamp"])


def downgrade() -> None:
    op.drop_table("order_book_snapshots")
    op.drop_table("funding_rates")
    op.drop_table("klines")
