"""Add cloud candidate, run, and service identity registry.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "platform_candidates",
        sa.Column("descriptor_hash", sa.String(64), nullable=False),
        sa.Column("git_sha", sa.String(40), nullable=False),
        sa.Column("schema_revision", sa.String(32), nullable=False),
        sa.Column("descriptor_json", jsonb, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("creator_deployment_id", sa.String(128), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qualifying_at", sa.DateTime(timezone=True)),
        sa.Column("qualified_at", sa.DateTime(timezone=True)),
        sa.Column("qualification_evidence_hash", sa.String(64)),
        sa.CheckConstraint(
            "descriptor_hash ~ '^[0-9a-f]{64}$' AND "
            "(qualification_evidence_hash IS NULL OR "
            "qualification_evidence_hash ~ '^[0-9a-f]{64}$')",
            name="ck_platform_candidate_hashes",
        ),
        sa.CheckConstraint(
            "git_sha ~ '^[0-9a-f]{40}$'",
            name="ck_platform_candidate_git_sha",
        ),
        sa.CheckConstraint(
            "schema_revision ~ '^[0-9]{4}$'",
            name="ck_platform_candidate_schema_revision",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(descriptor_json) = 'object'",
            name="ck_platform_candidate_json",
        ),
        sa.CheckConstraint(
            "creator_deployment_id <> ''",
            name="ck_platform_candidate_identity_fields",
        ),
        sa.CheckConstraint(
            "status IN ('registered', 'qualifying', 'qualified', 'rejected')",
            name="ck_platform_candidate_status",
        ),
        sa.CheckConstraint(
            "(status = 'registered' AND qualifying_at IS NULL AND qualified_at IS NULL AND "
            "qualification_evidence_hash IS NULL) OR "
            "(status = 'qualifying' AND qualifying_at IS NOT NULL AND qualified_at IS NULL AND "
            "qualification_evidence_hash IS NULL) OR "
            "(status IN ('qualified', 'rejected') AND qualifying_at IS NOT NULL AND "
            "qualified_at IS NOT NULL AND qualification_evidence_hash IS NOT NULL)",
            name="ck_platform_candidate_lifecycle",
        ),
        sa.CheckConstraint(
            "(qualifying_at IS NULL OR qualifying_at >= registered_at) AND "
            "(qualified_at IS NULL OR qualified_at >= qualifying_at)",
            name="ck_platform_candidate_time_order",
        ),
        sa.PrimaryKeyConstraint("descriptor_hash"),
    )
    op.create_index(
        "ix_platform_candidates_status_registered",
        "platform_candidates",
        ["status", "registered_at"],
    )

    op.create_table(
        "run_instances",
        sa.Column("id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("database_system_identifier", sa.String(32), nullable=False),
        sa.Column("railway_environment_id", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_operator_command_id", uuid),
        sa.Column("activating_worker_boot_id", uuid),
        sa.Column("continuity_invalidated", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("invalidation_reason", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_instance_hashes",
        ),
        sa.CheckConstraint(
            "database_system_identifier ~ '^[0-9]{1,32}$' AND railway_environment_id <> ''",
            name="ck_run_instance_database_identity",
        ),
        sa.CheckConstraint(
            "purpose IN ('process_drill', 'soak', 'seven_day')",
            name="ck_run_instance_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('standby', 'active', 'invalidated', 'completed')",
            name="ck_run_instance_status",
        ),
        sa.CheckConstraint(
            "(status = 'standby' AND requested_operator_command_id IS NULL AND "
            "started_at IS NULL AND "
            "activating_worker_boot_id IS NULL AND NOT continuity_invalidated AND "
            "invalidated_at IS NULL AND invalidation_reason IS NULL) OR "
            "(status = 'active' AND started_at IS NOT NULL AND "
            "requested_operator_command_id IS NOT NULL AND "
            "activating_worker_boot_id IS NOT NULL AND NOT continuity_invalidated AND "
            "invalidated_at IS NULL AND invalidation_reason IS NULL) OR "
            "(status = 'invalidated' AND continuity_invalidated AND "
            "invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL AND "
            "((started_at IS NULL AND requested_operator_command_id IS NULL AND "
            "activating_worker_boot_id IS NULL) OR "
            "(started_at IS NOT NULL AND requested_operator_command_id IS NOT NULL AND "
            "activating_worker_boot_id IS NOT NULL))) OR "
            "(status = 'completed' AND started_at IS NOT NULL AND "
            "requested_operator_command_id IS NOT NULL AND "
            "activating_worker_boot_id IS NOT NULL AND NOT continuity_invalidated AND "
            "invalidated_at IS NULL AND invalidation_reason IS NULL)",
            name="ck_run_instance_lifecycle",
        ),
        sa.CheckConstraint(
            "(started_at IS NULL OR started_at >= created_at) AND "
            "(invalidated_at IS NULL OR invalidated_at >= COALESCE(started_at, created_at))",
            name="ck_run_instance_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_run_instance_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_hash"],
            ["platform_candidates.descriptor_hash"],
            name="fk_run_instance_candidate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_operator_command_id"],
            ["operator_commands.id"],
            name="fk_run_instance_operator_command",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", name="uq_run_instance_experiment"),
        sa.UniqueConstraint(
            "requested_operator_command_id",
            name="uq_run_instance_operator_command",
        ),
        sa.UniqueConstraint(
            "activating_worker_boot_id",
            name="uq_run_instance_activating_boot",
        ),
    )
    op.create_index(
        "ix_run_instances_environment_status",
        "run_instances",
        ["railway_environment_id", "status", "created_at"],
    )
    op.create_index(
        "uq_run_instances_active_environment",
        "run_instances",
        ["railway_environment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "service_instances",
        sa.Column("boot_id", uuid, nullable=False),
        sa.Column("run_id", uuid),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("environment_id", sa.String(128), nullable=False),
        sa.Column("service_id", sa.String(128), nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(128)),
        sa.Column("replica_id", sa.String(128), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("service_role", sa.String(16), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("runtime_identity_json", jsonb, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_sequence", sa.Integer(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("terminal_reason", sa.String(1000)),
        sa.CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$'",
            name="ck_service_instance_candidate_hash",
        ),
        sa.CheckConstraint(
            "service_role IN ('web', 'worker', 'operations', 'verifier', 'migrator')",
            name="ck_service_instance_role",
        ),
        sa.CheckConstraint(
            "project_id <> '' AND environment_id <> '' AND service_id <> '' AND "
            "deployment_id <> '' AND replica_id <> '' AND region <> ''",
            name="ck_service_instance_identity_fields",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(runtime_identity_json) = 'object'",
            name="ck_service_instance_runtime_json",
        ),
        sa.CheckConstraint(
            "heartbeat_sequence >= 0",
            name="ck_service_instance_heartbeat_sequence",
        ),
        sa.CheckConstraint(
            "started_at <= first_seen_at AND first_seen_at <= last_heartbeat_at AND "
            "(stopped_at IS NULL OR stopped_at >= last_heartbeat_at)",
            name="ck_service_instance_time_order",
        ),
        sa.CheckConstraint(
            "(stopped_at IS NULL AND terminal_reason IS NULL) OR "
            "(stopped_at IS NOT NULL AND terminal_reason IS NOT NULL AND "
            "terminal_reason <> '')",
            name="ck_service_instance_terminal_state",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_service_instance_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_hash"],
            ["platform_candidates.descriptor_hash"],
            name="fk_service_instance_candidate",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("boot_id"),
        sa.UniqueConstraint("boot_id", "run_id", name="uq_service_instance_boot_run"),
    )
    op.create_index(
        "ix_service_instances_run_role_heartbeat",
        "service_instances",
        ["run_id", "service_role", "last_heartbeat_at"],
    )
    op.create_foreign_key(
        "fk_run_instance_activating_worker_boot",
        "run_instances",
        "service_instances",
        ["activating_worker_boot_id", "id"],
        ["boot_id", "run_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_run_instance_activating_worker_boot",
        "run_instances",
        type_="foreignkey",
    )
    op.drop_table("service_instances")
    op.drop_table("run_instances")
    op.drop_table("platform_candidates")
