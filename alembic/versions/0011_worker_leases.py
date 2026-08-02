"""Add crash-safe worker ownership leases.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "worker_leases",
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("worker_id", uuid, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("epoch > 0", name="ck_worker_lease_epoch_positive"),
        sa.CheckConstraint(
            "status IN ('active', 'released')",
            name="ck_worker_lease_status",
        ),
        sa.CheckConstraint(
            "heartbeat_at >= acquired_at",
            name="ck_worker_lease_heartbeat_order",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND released_at IS NULL AND expires_at > heartbeat_at) OR "
            "(status = 'released' AND released_at IS NOT NULL "
            "AND released_at >= heartbeat_at)",
            name="ck_worker_lease_lifecycle",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("experiment_id"),
    )
    op.create_index(
        "ix_worker_leases_status_expiry",
        "worker_leases",
        ["status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_leases_status_expiry", table_name="worker_leases")
    op.drop_table("worker_leases")
