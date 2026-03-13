"""Baseline — empty starting point.

Revision ID: 0001
Revises:
Create Date: 2026-03-12

Real tables are added in:
  - Batch 1: market data tables (market_data layer)
  - Batch 2: trade record table, compliance ledger (execution layer)
"""

from typing import Sequence, Union

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
