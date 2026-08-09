"""Add exactly-once operations and immutable artifact catalog.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "scheduled_operations",
        sa.Column("id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("operation_type", sa.String(32), nullable=False),
        sa.Column("berlin_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner_boot_id", uuid, nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("result_artifact_ids", jsonb, nullable=False),
        sa.Column("reason_code", sa.String(128)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            f"operation_type IN ({_OPERATION_TYPES})",
            name="ck_scheduled_operation_type",
        ),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_scheduled_operation_status",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_scheduled_operation_attempt"),
        sa.CheckConstraint(
            "jsonb_typeof(result_artifact_ids) = 'array'",
            name="ck_scheduled_operation_results_json",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_scheduled_operation_hash",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND reason_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND reason_code IS NOT NULL "
            "AND reason_code <> '') OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL AND reason_code IS NULL "
            "AND jsonb_array_length(result_artifact_ids) > 0)",
            name="ck_scheduled_operation_lifecycle",
        ),
        sa.CheckConstraint(
            "started_at >= generated_at AND (completed_at IS NULL OR completed_at >= started_at)",
            name="ck_scheduled_operation_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_scheduled_operation_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_scheduled_operation_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_boot_id", "run_id"],
            ["service_instances.boot_id", "service_instances.run_id"],
            name="fk_scheduled_operation_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "operation_type",
            "berlin_date",
            name="uq_scheduled_operation_key",
        ),
    )
    op.create_index(
        "ix_scheduled_operations_run_status_date",
        "scheduled_operations",
        ["run_id", "status", "berlin_date"],
    )

    op.create_table(
        "artifact_publication_attempts",
        sa.Column("id", uuid, nullable=False),
        sa.Column("operation_id", uuid, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("bundle_content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("reason_code", sa.String(128)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="ck_artifact_attempt_number"),
        sa.CheckConstraint(
            "bundle_content_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_artifact_attempt_hashes",
        ),
        sa.CheckConstraint(
            "status IN ('started','succeeded','failed')",
            name="ck_artifact_attempt_status",
        ),
        sa.CheckConstraint(
            "(status = 'started' AND completed_at IS NULL AND reason_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND reason_code IS NOT NULL "
            "AND reason_code <> '') OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL AND reason_code IS NULL)",
            name="ck_artifact_attempt_lifecycle",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_artifact_attempt_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["scheduled_operations.id"],
            name="fk_artifact_attempt_operation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "attempt",
            name="uq_artifact_attempt_sequence",
        ),
    )
    op.create_index(
        "ix_artifact_attempts_operation_status",
        "artifact_publication_attempts",
        ["operation_id", "status", "attempt"],
    )

    op.create_table(
        "artifact_records",
        sa.Column("id", uuid, nullable=False),
        sa.Column("operation_id", uuid, nullable=False),
        sa.Column("publication_attempt_id", uuid, nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("experiment_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("report_id", sa.String(128), nullable=False),
        sa.Column("bundle_content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producing_deployment_id", sa.String(128), nullable=False),
        sa.Column("producing_service_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("replica_inventory", jsonb, nullable=False),
        sa.Column("canonical_inventory", jsonb, nullable=False),
        sa.Column("previous_evidence_hash", sa.String(64), nullable=False),
        sa.Column("catalog_content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "environment IN ('qualification','production')",
            name="ck_artifact_record_environment",
        ),
        sa.CheckConstraint(
            f"artifact_type IN ({_ARTIFACT_TYPES})",
            name="ck_artifact_record_type",
        ),
        sa.CheckConstraint(
            f"media_type IN ({_MEDIA_TYPES})",
            name="ck_artifact_record_media_type",
        ),
        sa.CheckConstraint(
            "candidate_hash ~ '^[0-9a-f]{64}$' AND "
            "bundle_content_hash ~ '^[0-9a-f]{64}$' AND "
            "previous_evidence_hash ~ '^[0-9a-f]{64}$' AND "
            "catalog_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_artifact_record_hashes",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0 AND sequence >= 1",
            name="ck_artifact_record_counts",
        ),
        sa.CheckConstraint(
            "report_id <> '' AND producing_deployment_id <> '' AND producing_service_id <> ''",
            name="ck_artifact_record_identity_fields",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(replica_inventory) = 'array' AND "
            "jsonb_array_length(replica_inventory) > 0 AND "
            "jsonb_typeof(canonical_inventory) = 'array' AND "
            "jsonb_array_length(canonical_inventory) > 0",
            name="ck_artifact_record_inventories_json",
        ),
        sa.CheckConstraint(
            "recorded_at >= generated_at",
            name="ck_artifact_record_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["scheduled_operations.id"],
            name="fk_artifact_record_operation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_attempt_id"],
            ["artifact_publication_attempts.id"],
            name="fk_artifact_record_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_hash"],
            ["platform_candidates.descriptor_hash"],
            name="fk_artifact_record_candidate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name="fk_artifact_record_experiment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_artifact_record_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("publication_attempt_id", name="uq_artifact_record_attempt"),
        sa.UniqueConstraint(
            "environment",
            "candidate_hash",
            "experiment_id",
            "artifact_type",
            "report_id",
            name="uq_artifact_record_report_identity",
        ),
        sa.UniqueConstraint(
            "environment",
            "candidate_hash",
            "experiment_id",
            "sequence",
            name="uq_artifact_record_stream_sequence",
        ),
    )
    op.create_index(
        "ix_artifact_records_run_type_generated",
        "artifact_records",
        ["run_id", "artifact_type", "generated_at"],
    )

    op.execute(
        """
        CREATE FUNCTION maais_validate_artifact_record_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.canonical_inventory) AS item
            WHERE COALESCE(item->>'version_id', '') = ''
               OR COALESCE(item->>'retention_mode', '') NOT IN ('GOVERNANCE', 'COMPLIANCE')
               OR COALESCE(item->>'retain_until', '') = ''
          ) THEN
            RAISE EXCEPTION 'canonical artifact inventory lacks version or retention evidence';
          END IF;
          IF NOT EXISTS (
            SELECT 1
            FROM artifact_publication_attempts AS attempt
            JOIN scheduled_operations AS operation ON operation.id = attempt.operation_id
            JOIN run_instances AS run ON run.id = NEW.run_id
            JOIN service_instances AS producer ON producer.boot_id = operation.owner_boot_id
            WHERE attempt.id = NEW.publication_attempt_id
              AND attempt.status = 'started'
              AND attempt.operation_id = NEW.operation_id
              AND attempt.bundle_content_hash = NEW.bundle_content_hash
              AND operation.status = 'running'
              AND operation.run_id = NEW.run_id
              AND operation.experiment_id = NEW.experiment_id
              AND operation.generated_at = NEW.generated_at
              AND run.experiment_id = NEW.experiment_id
              AND run.candidate_hash = NEW.candidate_hash
              AND producer.run_id = NEW.run_id
              AND producer.candidate_hash = NEW.candidate_hash
              AND producer.deployment_id = NEW.producing_deployment_id
              AND producer.service_id = NEW.producing_service_id
              AND producer.stopped_at IS NULL
          ) THEN
            RAISE EXCEPTION 'artifact record authority identity is inconsistent';
          END IF;
          IF NEW.sequence = 1 THEN
            IF NEW.previous_evidence_hash <> repeat('0', 64)
               OR EXISTS (
                 SELECT 1 FROM artifact_records
                 WHERE environment = NEW.environment
                   AND candidate_hash = NEW.candidate_hash
                   AND experiment_id = NEW.experiment_id
               ) THEN
              RAISE EXCEPTION 'artifact record genesis is invalid';
            END IF;
          ELSIF NOT EXISTS (
            SELECT 1 FROM artifact_records
            WHERE environment = NEW.environment
              AND candidate_hash = NEW.candidate_hash
              AND experiment_id = NEW.experiment_id
              AND sequence = NEW.sequence - 1
              AND catalog_content_hash = NEW.previous_evidence_hash
          ) THEN
            RAISE EXCEPTION 'artifact record previous evidence hash is invalid';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_artifact_record_validate_insert
        BEFORE INSERT ON artifact_records
        FOR EACH ROW EXECUTE FUNCTION maais_validate_artifact_record_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION maais_reject_artifact_record_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          RAISE EXCEPTION 'artifact records are append-only';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_artifact_record_immutable
        BEFORE UPDATE OR DELETE ON artifact_records
        FOR EACH ROW EXECUTE FUNCTION maais_reject_artifact_record_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION maais_guard_artifact_attempt_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'artifact publication attempts are append-only';
          END IF;
          IF OLD.status <> 'started'
             OR NEW.status NOT IN ('succeeded', 'failed')
             OR NEW.id IS DISTINCT FROM OLD.id
             OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
             OR NEW.attempt IS DISTINCT FROM OLD.attempt
             OR NEW.bundle_content_hash IS DISTINCT FROM OLD.bundle_content_hash
             OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION 'artifact publication attempt mutation is not allowed';
          END IF;
          IF NEW.status = 'succeeded'
             AND NOT EXISTS (
               SELECT 1 FROM artifact_records
               WHERE publication_attempt_id = NEW.id
             ) THEN
            RAISE EXCEPTION 'successful publication attempt requires a catalog record';
          END IF;
          IF NEW.status = 'failed'
             AND EXISTS (
               SELECT 1 FROM artifact_records
               WHERE publication_attempt_id = NEW.id
             ) THEN
            RAISE EXCEPTION 'cataloged publication attempt cannot fail';
          END IF;
          RETURN NEW;
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_artifact_attempt_guard
        BEFORE UPDATE OR DELETE ON artifact_publication_attempts
        FOR EACH ROW EXECUTE FUNCTION maais_guard_artifact_attempt_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION maais_guard_scheduled_operation_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'scheduled operations cannot be deleted';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.run_id IS DISTINCT FROM OLD.run_id
             OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
             OR NEW.operation_type IS DISTINCT FROM OLD.operation_type
             OR NEW.berlin_date IS DISTINCT FROM OLD.berlin_date
             OR NEW.generated_at IS DISTINCT FROM OLD.generated_at
             OR OLD.status = 'succeeded' THEN
            RAISE EXCEPTION 'scheduled operation immutable evidence changed';
          END IF;
          IF OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed') THEN
            IF NEW.owner_boot_id IS DISTINCT FROM OLD.owner_boot_id
               OR NEW.attempt IS DISTINCT FROM OLD.attempt
               OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
              RAISE EXCEPTION 'scheduled operation terminal transition changed ownership';
            END IF;
            RETURN NEW;
          END IF;
          IF NEW.status = 'running' AND NEW.attempt = OLD.attempt + 1 THEN
            IF NEW.result_artifact_ids IS DISTINCT FROM OLD.result_artifact_ids THEN
              RAISE EXCEPTION 'scheduled operation retry changed verified results';
            END IF;
            IF NEW.owner_boot_id IS NOT DISTINCT FROM OLD.owner_boot_id THEN
              IF OLD.status <> 'failed' THEN
                RAISE EXCEPTION 'active scheduled operation cannot be restarted';
              END IF;
            ELSIF NOT EXISTS (
              SELECT 1 FROM service_instances
              WHERE boot_id = OLD.owner_boot_id
                AND stopped_at IS NOT NULL
                AND stopped_at <= NEW.started_at
            ) THEN
              RAISE EXCEPTION 'scheduled operation takeover requires a stopped owner';
            END IF;
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'scheduled operation transition is not allowed';
        END
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_scheduled_operation_guard
        BEFORE UPDATE OR DELETE ON scheduled_operations
        FOR EACH ROW EXECUTE FUNCTION maais_guard_scheduled_operation_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_scheduled_operation_guard ON scheduled_operations")
    op.execute("DROP FUNCTION IF EXISTS maais_guard_scheduled_operation_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_artifact_attempt_guard ON artifact_publication_attempts")
    op.execute("DROP FUNCTION IF EXISTS maais_guard_artifact_attempt_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_artifact_record_immutable ON artifact_records")
    op.execute("DROP FUNCTION IF EXISTS maais_reject_artifact_record_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_artifact_record_validate_insert ON artifact_records")
    op.execute("DROP FUNCTION IF EXISTS maais_validate_artifact_record_insert()")
    op.drop_table("artifact_records")
    op.drop_table("artifact_publication_attempts")
    op.drop_table("scheduled_operations")
