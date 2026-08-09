"""Append-only operational audit and health evidence projections."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maais.db.connection import Base
from maais.domain.json import MutableJsonValue

_WEB_EVENTS = (
    "'auth.csrf.rejected','auth.login.locked','auth.login.rejected',"
    "'auth.login.succeeded','auth.logout','auth.session.expired',"
    "'auth.session.revoked','operator.command.enqueued'"
)
_WORKER_EVENTS = (
    "'operator.command.accepted','operator.command.completed','operator.command.rejected',"
    "'run.completed','run.invalidated','run.started','service.booted','service.stopped'"
)
_OPERATIONS_EVENTS = (
    "'artifact.publication_failed','artifact.published','backup.failed','backup.succeeded',"
    "'daily_close.failed','daily_close.succeeded','health.evaluated','readiness.verdict',"
    "'restore.failed','restore.succeeded','service.booted','service.stopped'"
)
_MIGRATOR_EVENTS = "'migration.completed','migration.started','service.booted','service.stopped'"


class AuditEventModel(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_audit_event_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["service_boot_id"],
            ["service_instances.boot_id"],
            name="fk_audit_event_service_boot",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("event_id", name="uq_audit_event_id"),
        CheckConstraint("sequence >= 1", name="ck_audit_event_sequence"),
        CheckConstraint(
            "(sequence = 1 AND previous_hash IS NULL) OR "
            "(sequence > 1 AND previous_hash ~ '^[0-9a-f]{64}$')",
            name="ck_audit_event_previous_hash",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_audit_event_content_hash",
        ),
        CheckConstraint(
            "source_role IN ('web','worker','operations','migrator')",
            name="ck_audit_event_source_role",
        ),
        CheckConstraint(
            "actor_reference ~ '^[a-z][a-z0-9_]{1,31}:[0-9a-f]{32}$' AND "
            "(session_reference IS NULL OR "
            "session_reference ~ '^session:[0-9a-f]{32}$')",
            name="ck_audit_event_references",
        ),
        CheckConstraint(
            "event_code ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$' AND "
            "(reason_code IS NULL OR "
            "reason_code ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$')",
            name="ck_audit_event_codes",
        ),
        CheckConstraint(
            f"(source_role = 'web' AND event_code IN ({_WEB_EVENTS})) OR "
            f"(source_role = 'worker' AND event_code IN ({_WORKER_EVENTS})) OR "
            f"(source_role = 'operations' AND event_code IN ({_OPERATIONS_EVENTS})) OR "
            f"(source_role = 'migrator' AND event_code IN ({_MIGRATOR_EVENTS}))",
            name="ck_audit_event_role_code",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object' AND pg_column_size(evidence_json) <= 65536",
            name="ck_audit_event_evidence_json",
        ),
        Index("ix_audit_events_occurred", "occurred_at", "sequence"),
        Index("ix_audit_events_run_sequence", "run_id", "sequence"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    source_role: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    session_reference: Mapped[str | None] = mapped_column(String(64))
    event_code: Mapped[str] = mapped_column(String(128), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    evidence_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column()
    service_boot_id: Mapped[UUID | None] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class HealthEvaluationModel(Base):
    __tablename__ = "health_evaluations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_health_evaluation_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["service_boot_id"],
            ["service_instances.boot_id"],
            name="fk_health_evaluation_service_boot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_health_evaluation_incident",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["recovery_of_evaluation_id"],
            ["health_evaluations.evaluation_id"],
            name="fk_health_evaluation_recovery",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", "checked_at", name="uq_health_evaluation_run_checked"),
        CheckConstraint(
            "overall_status IN ('healthy','warning','critical') AND "
            "severity IN ('info','warning','critical')",
            name="ck_health_evaluation_status",
        ),
        CheckConstraint(
            "jsonb_typeof(failed_check_names) = 'array' AND "
            "jsonb_typeof(component_json) = 'object' AND "
            "component_json <> '{}'::jsonb AND "
            "pg_column_size(component_json) <= 131072",
            name="ck_health_evaluation_json",
        ),
        CheckConstraint(
            "(overall_status = 'healthy' AND severity = 'info' AND "
            "jsonb_array_length(failed_check_names) = 0) OR "
            "(overall_status = 'warning' AND severity = 'warning' AND "
            "jsonb_array_length(failed_check_names) > 0) OR "
            "(overall_status = 'critical' AND severity = 'critical' AND "
            "jsonb_array_length(failed_check_names) > 0)",
            name="ck_health_evaluation_lifecycle",
        ),
        CheckConstraint(
            "deduplication_key ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_health_evaluation_hashes",
        ),
        CheckConstraint(
            "(recovery_of_evaluation_id IS NULL AND recovered_at IS NULL) OR "
            "(recovery_of_evaluation_id IS NOT NULL AND recovered_at = checked_at AND "
            "overall_status = 'healthy' AND incident_id IS NULL AND "
            "recovery_of_evaluation_id <> evaluation_id)",
            name="ck_health_evaluation_recovery_state",
        ),
        Index("ix_health_evaluations_run_checked", "run_id", "checked_at"),
        Index("ix_health_evaluations_dedup_checked", "deduplication_key", "checked_at"),
    )

    evaluation_id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(nullable=False)
    service_boot_id: Mapped[UUID] = mapped_column(nullable=False)
    overall_status: Mapped[str] = mapped_column(String(16), nullable=False)
    failed_check_names: Mapped[list[MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID | None] = mapped_column()
    recovery_of_evaluation_id: Mapped[UUID | None] = mapped_column()
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    component_json: Mapped[dict[str, MutableJsonValue]] = mapped_column(JSONB, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
