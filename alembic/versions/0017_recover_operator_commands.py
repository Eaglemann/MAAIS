"""Allow accepted operator commands to record worker recovery.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RECOVERABLE_LIFECYCLE = (
    "(status = 'requested' AND version = 1 AND accepted_at IS NULL AND "
    "accepted_by IS NULL AND completed_at IS NULL AND result_json IS NULL) OR "
    "(status = 'accepted' AND version >= 2 AND accepted_at IS NOT NULL AND "
    "accepted_by IS NOT NULL AND completed_at IS NULL AND result_json IS NULL) OR "
    "(status IN ('completed', 'rejected') AND version >= 3 AND accepted_at IS NOT NULL "
    "AND accepted_by IS NOT NULL AND completed_at IS NOT NULL AND result_json IS NOT NULL)"
)

_FIXED_LIFECYCLE = (
    "(status = 'requested' AND version = 1 AND accepted_at IS NULL AND "
    "accepted_by IS NULL AND completed_at IS NULL AND result_json IS NULL) OR "
    "(status = 'accepted' AND version = 2 AND accepted_at IS NOT NULL AND "
    "accepted_by IS NOT NULL AND completed_at IS NULL AND result_json IS NULL) OR "
    "(status IN ('completed', 'rejected') AND version = 3 AND accepted_at IS NOT NULL "
    "AND accepted_by IS NOT NULL AND completed_at IS NOT NULL AND result_json IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_operator_command_lifecycle",
        "operator_commands",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operator_command_lifecycle",
        "operator_commands",
        _RECOVERABLE_LIFECYCLE,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_operator_command_lifecycle",
        "operator_commands",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operator_command_lifecycle",
        "operator_commands",
        _FIXED_LIFECYCLE,
    )
