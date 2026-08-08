"""Durable scheduled-operation and immutable artifact catalog projections."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue

_OPERATION_TYPES = (
    "'daily_close','daily_report','logical_backup','audit_export','artifact_publication',"
    "'qualification','restore_drill','process_drill','preflight','soak_verdict','final_report'"
)
_ARTIFACT_TYPES = (
    "'qualification_working','daily_report','audit_export','logical_backup','manifest',"
    "'qualification_evidence','restore_drill','process_drill','preflight','soak_verdict',"
    "'final_report'"
)
_MEDIA_TYPES = (
    "'application/gzip','application/json','application/octet-stream',"
    "'application/vnd.apache.parquet','application/x-ndjson','application/zip',"
    "'application/zstd','text/csv','text/markdown','text/plain'"
)


class ScheduledOperationModel(Base):
    __tablename__ = "scheduled_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_scheduled_operation_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_scheduled_operation_experiment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_boot_id", "run_id"],
            ["service_instances.boot_id", "service_instances.run_id"],
            name="fk_scheduled_operation_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "run_id",
            "operation_type",
            "berlin_date",
            name="uq_scheduled_operation_key",
        ),
        CheckConstraint(
            f"operation_type IN ({_OPERATION_TYPES})",
            name="ck_scheduled_operation_type",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_scheduled_operation_status",
        ),
        CheckConstraint(
            "attempt >= 1",
            name="ck_scheduled_operation_attempt",
        ),
        CheckConstraint(
            "jsonb_typeof(result_artifact_ids) = 'array'",
            name="ck_scheduled_operation_results_json",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_scheduled_operation_hash",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND reason_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND reason_code IS NOT NULL "
            "AND reason_code <> '') OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL AND reason_code IS NULL "
            "AND jsonb_array_length(result_artifact_ids) > 0)",
            name="ck_scheduled_operation_lifecycle",
        ),
        CheckConstraint(
            "started_at >= generated_at AND (completed_at IS NULL OR completed_at >= started_at)",
            name="ck_scheduled_operation_time_order",
        ),
        Index(
            "ix_scheduled_operations_run_status_date",
            "run_id",
            "status",
            "berlin_date",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    experiment_id: Mapped[UUID] = mapped_column(nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    berlin_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_boot_id: Mapped[UUID] = mapped_column(nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    result_artifact_ids: Mapped[list[MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ArtifactPublicationAttemptModel(Base):
    __tablename__ = "artifact_publication_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id"],
            ["scheduled_operations.id"],
            name="fk_artifact_attempt_operation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "operation_id",
            "attempt",
            name="uq_artifact_attempt_sequence",
        ),
        CheckConstraint("attempt >= 1", name="ck_artifact_attempt_number"),
        CheckConstraint(
            "bundle_content_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_artifact_attempt_hashes",
        ),
        CheckConstraint(
            "status IN ('started','succeeded','failed')",
            name="ck_artifact_attempt_status",
        ),
        CheckConstraint(
            "(status = 'started' AND completed_at IS NULL AND reason_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND reason_code IS NOT NULL "
            "AND reason_code <> '') OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL AND reason_code IS NULL)",
            name="ck_artifact_attempt_lifecycle",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_artifact_attempt_time_order",
        ),
        Index(
            "ix_artifact_attempts_operation_status",
            "operation_id",
            "status",
            "attempt",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ArtifactRecordModel(Base):
    __tablename__ = "artifact_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["operation_id"],
            ["scheduled_operations.id"],
            name="fk_artifact_record_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["publication_attempt_id"],
            ["artifact_publication_attempts.id"],
            name="fk_artifact_record_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["candidate_hash"],
            ["platform_candidates.descriptor_hash"],
            name="fk_artifact_record_candidate",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_artifact_record_experiment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_artifact_record_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "publication_attempt_id",
            name="uq_artifact_record_attempt",
        ),
        UniqueConstraint(
            "environment",
            "candidate_hash",
            "experiment_id",
            "artifact_type",
            "report_id",
            name="uq_artifact_record_report_identity",
        ),
        UniqueConstraint(
            "environment",
            "candidate_hash",
            "experiment_id",
            "sequence",
            name="uq_artifact_record_stream_sequence",
        ),
        CheckConstraint(
            "environment IN ('qualification','production')",
            name="ck_artifact_record_environment",
        ),
        CheckConstraint(
            f"artifact_type IN ({_ARTIFACT_TYPES})",
            name="ck_artifact_record_type",
        ),
        CheckConstraint(
            f"media_type IN ({_MEDIA_TYPES})",
            name="ck_artifact_record_media_type",
        ),
        CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$' AND "
            "bundle_content_hash ~ '^[0-9a-f]{64}$' AND "
            "previous_evidence_hash ~ '^[0-9a-f]{64}$' AND "
            "catalog_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_artifact_record_hashes",
        ),
        CheckConstraint(
            "size_bytes >= 0 AND sequence >= 1",
            name="ck_artifact_record_counts",
        ),
        CheckConstraint(
            "report_id <> '' AND producing_deployment_id <> '' AND producing_service_id <> ''",
            name="ck_artifact_record_identity_fields",
        ),
        CheckConstraint(
            "jsonb_typeof(replica_inventory) = 'array' AND "
            "jsonb_array_length(replica_inventory) > 0 AND "
            "jsonb_typeof(canonical_inventory) = 'array' AND "
            "jsonb_array_length(canonical_inventory) > 0",
            name="ck_artifact_record_inventories_json",
        ),
        CheckConstraint(
            "recorded_at >= generated_at",
            name="ck_artifact_record_time_order",
        ),
        Index(
            "ix_artifact_records_run_type_generated",
            "run_id",
            "artifact_type",
            "generated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    publication_attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    report_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bundle_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    producing_deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    producing_service_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    replica_inventory: Mapped[list[MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    canonical_inventory: Mapped[list[MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    previous_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
