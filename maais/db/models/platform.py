"""Cloud candidate, official-run, and service-boot authority projections."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue


class PlatformCandidateModel(Base):
    __tablename__ = "platform_candidates"
    __table_args__ = (
        CheckConstraint(
            "descriptor_hash ~ '^[0-9a-f]{64}$' AND "
            "(qualification_evidence_hash IS NULL OR "
            "qualification_evidence_hash ~ '^[0-9a-f]{64}$')",
            name="ck_platform_candidate_hashes",
        ),
        CheckConstraint(
            "git_sha ~ '^[0-9a-f]{40}$'",
            name="ck_platform_candidate_git_sha",
        ),
        CheckConstraint(
            "schema_revision ~ '^[0-9]{4}$'",
            name="ck_platform_candidate_schema_revision",
        ),
        CheckConstraint(
            "jsonb_typeof(descriptor_json) = 'object'",
            name="ck_platform_candidate_json",
        ),
        CheckConstraint(
            "creator_deployment_id <> ''",
            name="ck_platform_candidate_identity_fields",
        ),
        CheckConstraint(
            "status IN ('registered', 'qualifying', 'qualified', 'rejected')",
            name="ck_platform_candidate_status",
        ),
        CheckConstraint(
            "(status = 'registered' AND qualifying_at IS NULL AND qualified_at IS NULL AND "
            "qualification_evidence_hash IS NULL) OR "
            "(status = 'qualifying' AND qualifying_at IS NOT NULL AND qualified_at IS NULL AND "
            "qualification_evidence_hash IS NULL) OR "
            "(status IN ('qualified', 'rejected') AND qualifying_at IS NOT NULL AND "
            "qualified_at IS NOT NULL AND qualification_evidence_hash IS NOT NULL)",
            name="ck_platform_candidate_lifecycle",
        ),
        CheckConstraint(
            "(qualifying_at IS NULL OR qualifying_at >= registered_at) AND "
            "(qualified_at IS NULL OR qualified_at >= qualifying_at)",
            name="ck_platform_candidate_time_order",
        ),
        Index("ix_platform_candidates_status_registered", "status", "registered_at"),
    )

    descriptor_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    git_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    descriptor_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    creator_deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qualifying_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualification_evidence_hash: Mapped[str | None] = mapped_column(String(64))


class RunInstanceModel(Base):
    __tablename__ = "run_instances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_run_instance_experiment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["candidate_hash"],
            ["platform_candidates.descriptor_hash"],
            name="fk_run_instance_candidate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["requested_operator_command_id"],
            ["operator_commands.id"],
            name="fk_run_instance_operator_command",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["activating_worker_boot_id", "id"],
            ["service_instances.boot_id", "service_instances.run_id"],
            name="fk_run_instance_activating_worker_boot",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint("experiment_id", name="uq_run_instance_experiment"),
        UniqueConstraint(
            "requested_operator_command_id",
            name="uq_run_instance_operator_command",
        ),
        UniqueConstraint(
            "activating_worker_boot_id",
            name="uq_run_instance_activating_boot",
        ),
        CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$' AND manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_instance_hashes",
        ),
        CheckConstraint(
            "database_system_identifier ~ '^[0-9]{1,32}$' AND railway_environment_id <> ''",
            name="ck_run_instance_database_identity",
        ),
        CheckConstraint(
            "purpose IN ('process_drill', 'soak', 'seven_day')",
            name="ck_run_instance_purpose",
        ),
        CheckConstraint(
            "status IN ('standby', 'active', 'invalidated', 'completed')",
            name="ck_run_instance_status",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "(started_at IS NULL OR started_at >= created_at) AND "
            "(invalidated_at IS NULL OR invalidated_at >= COALESCE(started_at, created_at))",
            name="ck_run_instance_time_order",
        ),
        Index(
            "ix_run_instances_environment_status",
            "railway_environment_id",
            "status",
            "created_at",
        ),
        Index(
            "uq_run_instances_active_environment",
            "railway_environment_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    experiment_id: Mapped[UUID] = mapped_column(nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    database_system_identifier: Mapped[str] = mapped_column(String(32), nullable=False)
    railway_environment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_operator_command_id: Mapped[UUID | None] = mapped_column()
    activating_worker_boot_id: Mapped[UUID | None] = mapped_column()
    continuity_invalidated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ServiceInstanceModel(Base):
    __tablename__ = "service_instances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_service_instance_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["candidate_hash"],
            ["platform_candidates.descriptor_hash"],
            name="fk_service_instance_candidate",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("boot_id", "run_id", name="uq_service_instance_boot_run"),
        CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$'",
            name="ck_service_instance_candidate_hash",
        ),
        CheckConstraint(
            "service_role IN ('web', 'worker', 'operations', 'verifier', 'migrator')",
            name="ck_service_instance_role",
        ),
        CheckConstraint(
            "project_id <> '' AND environment_id <> '' AND service_id <> '' AND "
            "deployment_id <> '' AND replica_id <> '' AND region <> ''",
            name="ck_service_instance_identity_fields",
        ),
        CheckConstraint(
            "jsonb_typeof(runtime_identity_json) = 'object'",
            name="ck_service_instance_runtime_json",
        ),
        CheckConstraint(
            "heartbeat_sequence >= 0",
            name="ck_service_instance_heartbeat_sequence",
        ),
        CheckConstraint(
            "started_at <= first_seen_at AND first_seen_at <= last_heartbeat_at AND "
            "(stopped_at IS NULL OR stopped_at >= last_heartbeat_at)",
            name="ck_service_instance_time_order",
        ),
        CheckConstraint(
            "(stopped_at IS NULL AND terminal_reason IS NULL) OR "
            "(stopped_at IS NOT NULL AND terminal_reason IS NOT NULL AND terminal_reason <> '')",
            name="ck_service_instance_terminal_state",
        ),
        Index(
            "ix_service_instances_run_role_heartbeat",
            "run_id",
            "service_role",
            "last_heartbeat_at",
        ),
    )

    boot_id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID | None] = mapped_column()
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    environment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    service_id: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(String(128))
    replica_id: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    service_role: Mapped[str] = mapped_column(String(16), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_identity_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(
        JSONB,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_reason: Mapped[str | None] = mapped_column(String(1000))
