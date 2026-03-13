"""Compliance tables: trade_lots, trade_records, post_trade_reasoning.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-12

Implements Rule 8 (11-field trade record) and Rule 9 (FIFO accounting).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── trade_lots: open FIFO position lots ───────────────────────────────────
    op.create_table(
        "trade_lots",
        sa.Column("trade_id", sa.String(36), primary_key=True),
        sa.Column("asset", sa.String(20), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("original_size", sa.Numeric(30, 8), nullable=False),
        sa.Column("remaining_size", sa.Numeric(30, 8), nullable=False),
        sa.Column("entry_fees", sa.Numeric(20, 8), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lots_asset_side_opened", "trade_lots", ["asset", "side", "opened_at"])

    # ── trade_records: completed trades, Rule 8 (11 fields) ──────────────────
    op.create_table(
        "trade_records",
        # Field 1
        sa.Column("trade_id", sa.String(36), primary_key=True),
        # Field 2
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        # Field 3
        sa.Column("asset", sa.String(20), nullable=False),
        # Field 4
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        # Field 5
        sa.Column("exit_price", sa.Numeric(20, 8), nullable=False),
        # Field 6
        sa.Column("position_size", sa.Numeric(30, 8), nullable=False),
        # Field 7
        sa.Column("fees", sa.Numeric(20, 8), nullable=False),
        # Field 8
        sa.Column("funding_paid_received", sa.Numeric(20, 10), nullable=False),
        # Field 9
        sa.Column("profit_loss", sa.Numeric(20, 8), nullable=False),
        # Field 10
        sa.Column("exchange_rate", sa.Numeric(20, 8), nullable=False),
        # Field 11
        sa.Column("strategy_id", sa.String(100), nullable=False),
        # Metadata
        sa.Column("position_side", sa.String(5), nullable=False),
        sa.Column("entry_lot_id", sa.String(36), nullable=False, server_default=""),
    )
    op.create_index("ix_trades_asset_ts", "trade_records", ["asset", "timestamp"])
    op.create_index("ix_trades_strategy", "trade_records", ["strategy_id"])

    # ── post_trade_reasoning: agent outputs + decision context ────────────────
    op.create_table(
        "post_trade_reasoning",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("trade_id", sa.String(36),
                  sa.ForeignKey("trade_records.trade_id"), nullable=False),
        sa.Column("agent_outputs", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("consensus", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("reasoning_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reasoning_trade", "post_trade_reasoning", ["trade_id"])


def downgrade() -> None:
    op.drop_table("post_trade_reasoning")
    op.drop_table("trade_records")
    op.drop_table("trade_lots")
