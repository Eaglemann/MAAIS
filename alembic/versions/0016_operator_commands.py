"""Add the audited operator command inbox.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "operator_commands",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("command_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("payload_json", jsonb, nullable=False),
        sa.Column("operator_confirmed", sa.Boolean(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_by", sa.String(128)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", jsonb),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "command_type IN ('start', 'pause', 'resume', 'stop', 'emergency_halt', "
            "'flatten', 'acknowledge_incident', 'resolve_incident', 'reset_kill_switch')",
            name="ck_operator_command_type",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'accepted', 'completed', 'rejected')",
            name="ck_operator_command_status",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 128 AND actor <> '' AND reason <> ''",
            name="ck_operator_command_identity_fields",
        ),
        sa.CheckConstraint(
            "char_length(request_hash) = 64 AND char_length(content_hash) = 64",
            name="ck_operator_command_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload_json) = 'object' AND "
            "(result_json IS NULL OR jsonb_typeof(result_json) = 'object')",
            name="ck_operator_command_json_objects",
        ),
        sa.CheckConstraint(
            "command_type NOT IN ('start', 'pause', 'resume', 'stop', 'emergency_halt', "
            "'flatten', 'resolve_incident', 'reset_kill_switch') OR operator_confirmed",
            name="ck_operator_command_safety_confirmation",
        ),
        sa.CheckConstraint(
            "(status = 'requested' AND version = 1 AND accepted_at IS NULL AND "
            "accepted_by IS NULL AND completed_at IS NULL AND result_json IS NULL) OR "
            "(status = 'accepted' AND version = 2 AND accepted_at IS NOT NULL AND "
            "accepted_by IS NOT NULL AND completed_at IS NULL AND result_json IS NULL) OR "
            "(status IN ('completed', 'rejected') AND version = 3 AND accepted_at IS NOT NULL "
            "AND accepted_by IS NOT NULL AND completed_at IS NOT NULL AND result_json IS NOT NULL)",
            name="ck_operator_command_lifecycle",
        ),
        sa.CheckConstraint(
            "(accepted_at IS NULL OR accepted_at >= requested_at) AND "
            "(completed_at IS NULL OR completed_at >= accepted_at)",
            name="ck_operator_command_time_order",
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id", "idempotency_key", name="uq_operator_command_idempotency"
        ),
    )
    op.create_index(
        "ix_operator_commands_experiment_status_time",
        "operator_commands",
        ["experiment_id", "status", "requested_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operator_commands_experiment_status_time",
        table_name="operator_commands",
    )
    op.drop_table("operator_commands")
