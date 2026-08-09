"""Add private operator sessions and global login throttle.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA maais_auth")
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "operator_sessions",
        sa.Column("id", uuid, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$' AND csrf_hash ~ '^[0-9a-f]{64}$' "
            "AND token_hash <> csrf_hash",
            name="ck_operator_session_hashes",
        ),
        sa.CheckConstraint(
            "actor <> '' AND actor = btrim(actor)",
            name="ck_operator_session_actor",
        ),
        sa.CheckConstraint("version >= 1", name="ck_operator_session_version"),
        sa.CheckConstraint(
            "created_at <= last_seen_at AND last_seen_at <= expires_at AND "
            "expires_at = created_at + INTERVAL '12 hours' AND "
            "(revoked_at IS NULL OR revoked_at >= last_seen_at)",
            name="ck_operator_session_time_order",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="maais_auth",
    )
    op.create_index(
        "uq_operator_sessions_token_hash",
        "operator_sessions",
        ["token_hash"],
        unique=True,
        schema="maais_auth",
    )
    op.create_index(
        "uq_operator_sessions_csrf_hash",
        "operator_sessions",
        ["csrf_hash"],
        unique=True,
        schema="maais_auth",
    )
    op.create_index(
        "ix_operator_sessions_active_expiry",
        "operator_sessions",
        ["expires_at", "last_seen_at"],
        schema="maais_auth",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "operator_auth_state",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_operator_auth_state_singleton"),
        sa.CheckConstraint(
            "failed_attempts BETWEEN 0 AND 5 AND version >= 1",
            name="ck_operator_auth_state_counts",
        ),
        sa.CheckConstraint(
            "(failed_attempts = 0 AND window_started_at IS NULL AND locked_until IS NULL) OR "
            "(failed_attempts BETWEEN 1 AND 4 AND window_started_at IS NOT NULL AND "
            "updated_at >= window_started_at AND locked_until IS NULL) OR "
            "(failed_attempts = 5 AND window_started_at IS NOT NULL AND "
            "updated_at >= window_started_at AND locked_until IS NOT NULL AND "
            "locked_until = updated_at + INTERVAL '30 minutes')",
            name="ck_operator_auth_state_lifecycle",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="maais_auth",
    )
    op.execute(
        "INSERT INTO maais_auth.operator_auth_state "
        "(id, failed_attempts, window_started_at, locked_until, updated_at, version) "
        "VALUES (1, 0, NULL, NULL, CURRENT_TIMESTAMP, 1)"
    )


def downgrade() -> None:
    op.drop_table("operator_auth_state", schema="maais_auth")
    op.drop_index(
        "ix_operator_sessions_active_expiry",
        table_name="operator_sessions",
        schema="maais_auth",
    )
    op.drop_index(
        "uq_operator_sessions_csrf_hash",
        table_name="operator_sessions",
        schema="maais_auth",
    )
    op.drop_index(
        "uq_operator_sessions_token_hash",
        table_name="operator_sessions",
        schema="maais_auth",
    )
    op.drop_table("operator_sessions", schema="maais_auth")
    op.execute("DROP SCHEMA maais_auth")
