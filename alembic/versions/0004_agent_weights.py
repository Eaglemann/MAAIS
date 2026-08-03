"""agent_weights table

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-13
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_weights",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_name", sa.String(64), nullable=False, unique=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    # Seed with equal weights for all 8 agents
    op.execute("""
        INSERT INTO agent_weights (agent_name, weight) VALUES
        ('momentum', 1.0),
        ('technical_structure', 1.0),
        ('liquidity', 1.0),
        ('order_flow_toxicity', 1.0),
        ('stop_run_detection', 1.0),
        ('mean_reversion', 1.0),
        ('carry_yield', 1.0),
        ('macro_sentiment', 1.0)
    """)


def downgrade() -> None:
    op.drop_table("agent_weights")
