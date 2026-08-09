"""Add append-only health and operational audit evidence.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
_FORBIDDEN_EVIDENCE_KEY_PATTERN = (
    "_(access_key|account_equity|authorization|balance|body|client_secret|cookie|"
    "credentials|csrf|database_url|headers|local_storage|order_quantity|password|position|"
    "positions|private_key|quantity|raw_response|request_body|response_body|secret|"
    "session_storage|session_token|token)_"
)


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "audit_events",
        sa.Column("sequence", sa.BigInteger(), nullable=False, autoincrement=False),
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("previous_hash", sa.String(64)),
        sa.Column("source_role", sa.String(16), nullable=False),
        sa.Column("actor_reference", sa.String(64), nullable=False),
        sa.Column("session_reference", sa.String(64)),
        sa.Column("event_code", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(128)),
        sa.Column("evidence_json", jsonb, nullable=False),
        sa.Column("run_id", uuid),
        sa.Column("service_boot_id", uuid),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_audit_event_sequence"),
        sa.CheckConstraint(
            "(sequence = 1 AND previous_hash IS NULL) OR "
            "(sequence > 1 AND previous_hash ~ '^[0-9a-f]{64}$')",
            name="ck_audit_event_previous_hash",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_audit_event_content_hash",
        ),
        sa.CheckConstraint(
            "source_role IN ('web','worker','operations','migrator')",
            name="ck_audit_event_source_role",
        ),
        sa.CheckConstraint(
            "actor_reference ~ '^[a-z][a-z0-9_]{1,31}:[0-9a-f]{32}$' AND "
            "(session_reference IS NULL OR "
            "session_reference ~ '^session:[0-9a-f]{32}$')",
            name="ck_audit_event_references",
        ),
        sa.CheckConstraint(
            "event_code ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$' AND "
            "(reason_code IS NULL OR "
            "reason_code ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$')",
            name="ck_audit_event_codes",
        ),
        sa.CheckConstraint(
            f"(source_role = 'web' AND event_code IN ({_WEB_EVENTS})) OR "
            f"(source_role = 'worker' AND event_code IN ({_WORKER_EVENTS})) OR "
            f"(source_role = 'operations' AND event_code IN ({_OPERATIONS_EVENTS})) OR "
            f"(source_role = 'migrator' AND event_code IN ({_MIGRATOR_EVENTS}))",
            name="ck_audit_event_role_code",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_json) = 'object' AND pg_column_size(evidence_json) <= 65536",
            name="ck_audit_event_evidence_json",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_audit_event_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_boot_id"],
            ["service_instances.boot_id"],
            name="fk_audit_event_service_boot",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("event_id", name="uq_audit_event_id"),
    )
    op.create_index(
        "ix_audit_events_occurred",
        "audit_events",
        ["occurred_at", "sequence"],
    )
    op.create_index(
        "ix_audit_events_run_sequence",
        "audit_events",
        ["run_id", "sequence"],
    )

    op.create_table(
        "health_evaluations",
        sa.Column("evaluation_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("service_boot_id", uuid, nullable=False),
        sa.Column("overall_status", sa.String(16), nullable=False),
        sa.Column("failed_check_names", jsonb, nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("deduplication_key", sa.String(64), nullable=False),
        sa.Column("incident_id", uuid),
        sa.Column("recovery_of_evaluation_id", uuid),
        sa.Column("recovered_at", sa.DateTime(timezone=True)),
        sa.Column("component_json", jsonb, nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "overall_status IN ('healthy','warning','critical') AND "
            "severity IN ('info','warning','critical')",
            name="ck_health_evaluation_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(failed_check_names) = 'array' AND "
            "jsonb_typeof(component_json) = 'object' AND "
            "component_json <> '{}'::jsonb AND "
            "pg_column_size(component_json) <= 131072",
            name="ck_health_evaluation_json",
        ),
        sa.CheckConstraint(
            "(overall_status = 'healthy' AND severity = 'info' AND "
            "jsonb_array_length(failed_check_names) = 0) OR "
            "(overall_status = 'warning' AND severity = 'warning' AND "
            "jsonb_array_length(failed_check_names) > 0) OR "
            "(overall_status = 'critical' AND severity = 'critical' AND "
            "jsonb_array_length(failed_check_names) > 0)",
            name="ck_health_evaluation_lifecycle",
        ),
        sa.CheckConstraint(
            "deduplication_key ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_health_evaluation_hashes",
        ),
        sa.CheckConstraint(
            "(recovery_of_evaluation_id IS NULL AND recovered_at IS NULL) OR "
            "(recovery_of_evaluation_id IS NOT NULL AND recovered_at = checked_at AND "
            "overall_status = 'healthy' AND incident_id IS NULL AND "
            "recovery_of_evaluation_id <> evaluation_id)",
            name="ck_health_evaluation_recovery_state",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["run_instances.id"],
            name="fk_health_evaluation_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["service_boot_id"],
            ["service_instances.boot_id"],
            name="fk_health_evaluation_service_boot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_health_evaluation_incident",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recovery_of_evaluation_id"],
            ["health_evaluations.evaluation_id"],
            name="fk_health_evaluation_recovery",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("evaluation_id"),
        sa.UniqueConstraint(
            "run_id",
            "checked_at",
            name="uq_health_evaluation_run_checked",
        ),
    )
    op.create_index(
        "ix_health_evaluations_run_checked",
        "health_evaluations",
        ["run_id", "checked_at"],
    )
    op.create_index(
        "ix_health_evaluations_dedup_checked",
        "health_evaluations",
        ["deduplication_key", "checked_at"],
    )

    op.execute(_UTC_ISO_FUNCTION_SQL)
    op.execute(_CANONICAL_JSON_FUNCTION_SQL)
    op.execute(_AUDIT_EVIDENCE_SAFE_FUNCTION_SQL)
    op.execute(_IMMUTABLE_TRIGGER_FUNCTION_SQL)
    op.execute(_AUDIT_GATEWAY_SQL)
    op.execute(
        "CREATE TRIGGER trg_audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION public._maais_reject_immutable_evidence_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_health_evaluations_immutable "
        "BEFORE UPDATE OR DELETE ON health_evaluations FOR EACH ROW "
        "EXECUTE FUNCTION public._maais_reject_immutable_evidence_mutation()"
    )
    op.execute(_AUDIT_PRIVILEGES_SQL)


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.maais_append_audit_event("
        "uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone)"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_health_evaluations_immutable ON health_evaluations")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events")
    op.drop_table("health_evaluations")
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION IF EXISTS public._maais_reject_immutable_evidence_mutation()")
    op.execute("DROP FUNCTION IF EXISTS public._maais_audit_evidence_safe(jsonb)")


_UTC_ISO_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION public._maais_utc_iso(p_value timestamp with time zone)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog, public
AS $maais_utc$
    SELECT CASE
        WHEN pg_catalog.date_part('microseconds', p_value)::bigint % 1000000 = 0
        THEN pg_catalog.to_char(p_value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') || 'Z'
        ELSE pg_catalog.to_char(p_value AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || 'Z'
    END
$maais_utc$;
""".strip()

_CANONICAL_JSON_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public._maais_canonical_jsonb(p_value jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog, public
AS $maais_json$
    SELECT CASE pg_catalog.jsonb_typeof(p_value)
        WHEN 'object' THEN '{' || COALESCE(
            (
                SELECT pg_catalog.string_agg(
                    pg_catalog.to_jsonb(entry.key)::text || ':' ||
                    public._maais_canonical_jsonb(entry.value),
                    ',' ORDER BY entry.key
                )
                FROM pg_catalog.jsonb_each(p_value) AS entry
            ),
            ''
        ) || '}'
        WHEN 'array' THEN '[' || COALESCE(
            (
                SELECT pg_catalog.string_agg(
                    public._maais_canonical_jsonb(entry.value),
                    ',' ORDER BY entry.ordinality
                )
                FROM pg_catalog.jsonb_array_elements(p_value)
                    WITH ORDINALITY AS entry(value, ordinality)
            ),
            ''
        ) || ']'
        ELSE p_value::text
    END
$maais_json$;
""".strip()

_AUDIT_EVIDENCE_SAFE_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION public._maais_audit_evidence_safe(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
STRICT
SET search_path = pg_catalog, public
AS $maais_safe$
DECLARE
    entry record;
    normalized_key text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) = 'object' THEN
        FOR entry IN SELECT key, value FROM pg_catalog.jsonb_each(p_value) LOOP
            normalized_key := pg_catalog.btrim(
                pg_catalog.regexp_replace(
                    pg_catalog.lower(entry.key), '[^a-z0-9]+', '_', 'g'
                ),
                '_'
            );
            IF ('_' || normalized_key || '_') ~
                '{_FORBIDDEN_EVIDENCE_KEY_PATTERN}' THEN
                RETURN false;
            END IF;
            IF NOT public._maais_audit_evidence_safe(entry.value) THEN
                RETURN false;
            END IF;
        END LOOP;
    ELSIF pg_catalog.jsonb_typeof(p_value) = 'array' THEN
        FOR entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_value) LOOP
            IF NOT public._maais_audit_evidence_safe(entry.value) THEN
                RETURN false;
            END IF;
        END LOOP;
    END IF;
    RETURN true;
END
$maais_safe$;
""".strip()

_IMMUTABLE_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public._maais_reject_immutable_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $maais_immutable$
BEGIN
    RAISE EXCEPTION 'immutable operational evidence cannot be updated or deleted'
        USING ERRCODE = '42501';
END
$maais_immutable$;
""".strip()

_AUDIT_GATEWAY_SQL = f"""
CREATE OR REPLACE FUNCTION public.maais_append_audit_event(
    p_event_id uuid,
    p_actor_reference text,
    p_session_reference text,
    p_event_code text,
    p_reason_code text,
    p_evidence jsonb,
    p_run_id uuid,
    p_service_boot_id uuid,
    p_occurred_at timestamp with time zone
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $maais_audit$
DECLARE
    source_role text;
    previous public.audit_events%ROWTYPE;
    existing public.audit_events%ROWTYPE;
    next_sequence bigint;
    next_previous_hash text;
    event_state jsonb;
    event_hash text;
BEGIN
    source_role := CASE session_user
        WHEN 'maais_migrator' THEN 'migrator'
        WHEN 'maais_web' THEN 'web'
        WHEN 'maais_worker' THEN 'worker'
        WHEN 'maais_ops' THEN 'operations'
        ELSE NULL
    END;
    IF source_role IS NULL THEN
        RAISE EXCEPTION 'audit gateway caller is not authorized' USING ERRCODE = '42501';
    END IF;
    IF (source_role = 'web' AND p_event_code NOT IN ({_WEB_EVENTS}))
        OR (source_role = 'worker' AND p_event_code NOT IN ({_WORKER_EVENTS}))
        OR (source_role = 'operations' AND p_event_code NOT IN ({_OPERATIONS_EVENTS}))
        OR (source_role = 'migrator' AND p_event_code NOT IN ({_MIGRATOR_EVENTS})) THEN
        RAISE EXCEPTION 'audit event code is not approved for caller' USING ERRCODE = '42501';
    END IF;
    IF p_event_id IS NULL OR p_event_id = '00000000-0000-0000-0000-000000000000'::uuid
        OR p_actor_reference !~ '^[a-z][a-z0-9_]{{1,31}}:[0-9a-f]{{32}}$'
        OR (p_session_reference IS NOT NULL AND
            p_session_reference !~ '^session:[0-9a-f]{{32}}$')
        OR p_event_code !~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'
        OR (p_reason_code IS NOT NULL AND
            p_reason_code !~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$')
        OR p_evidence IS NULL OR pg_catalog.jsonb_typeof(p_evidence) <> 'object'
        OR pg_catalog.pg_column_size(p_evidence) > 65536
        OR NOT public._maais_audit_evidence_safe(p_evidence)
        OR p_occurred_at IS NULL THEN
        RAISE EXCEPTION 'audit event arguments are invalid' USING ERRCODE = '22023';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('maais:audit-chain:v1', 22002)
    );
    SELECT * INTO existing FROM public.audit_events
    WHERE event_id = p_event_id FOR UPDATE;
    IF existing.event_id IS NOT NULL THEN
        IF existing.source_role <> source_role
            OR existing.actor_reference <> p_actor_reference
            OR existing.session_reference IS DISTINCT FROM p_session_reference
            OR existing.event_code <> p_event_code
            OR existing.reason_code IS DISTINCT FROM p_reason_code
            OR existing.evidence_json <> p_evidence
            OR existing.run_id IS DISTINCT FROM p_run_id
            OR existing.service_boot_id IS DISTINCT FROM p_service_boot_id
            OR existing.occurred_at <> p_occurred_at THEN
            RAISE EXCEPTION 'immutable audit event identity has changed'
                USING ERRCODE = '23505';
        END IF;
        RETURN existing.sequence;
    END IF;

    SELECT * INTO previous FROM public.audit_events ORDER BY sequence DESC LIMIT 1;
    next_sequence := COALESCE(previous.sequence + 1, 1);
    next_previous_hash := previous.content_hash;
    event_state := pg_catalog.jsonb_build_object(
        'actor_reference', p_actor_reference,
        'event_code', p_event_code,
        'event_id', p_event_id::text,
        'evidence', p_evidence,
        'occurred_at', public._maais_utc_iso(p_occurred_at),
        'previous_hash', next_previous_hash,
        'reason_code', p_reason_code,
        'run_id', CASE WHEN p_run_id IS NULL THEN NULL ELSE p_run_id::text END,
        'sequence', next_sequence,
        'service_boot_id',
            CASE WHEN p_service_boot_id IS NULL THEN NULL ELSE p_service_boot_id::text END,
        'session_reference', p_session_reference,
        'source_role', source_role
    );
    event_hash := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(public._maais_canonical_jsonb(event_state), 'UTF8')
        ),
        'hex'
    );
    INSERT INTO public.audit_events (
        sequence, event_id, previous_hash, source_role, actor_reference,
        session_reference, event_code, reason_code, evidence_json, run_id,
        service_boot_id, occurred_at, content_hash
    ) VALUES (
        next_sequence, p_event_id, next_previous_hash, source_role, p_actor_reference,
        p_session_reference, p_event_code, p_reason_code, p_evidence, p_run_id,
        p_service_boot_id, p_occurred_at, event_hash
    );
    RETURN next_sequence;
END
$maais_audit$;
""".strip()

_AUDIT_PRIVILEGES_SQL = """
REVOKE ALL ON FUNCTION public._maais_utc_iso(timestamp with time zone) FROM PUBLIC;
REVOKE ALL ON FUNCTION public._maais_canonical_jsonb(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public._maais_audit_evidence_safe(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public._maais_reject_immutable_evidence_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maais_append_audit_event(
    uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
) FROM PUBLIC;
DO $maais_audit_grants$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maais_web') THEN
        REVOKE ALL ON TABLE public.audit_events, public.health_evaluations FROM maais_web;
        GRANT SELECT ON TABLE public.audit_events, public.health_evaluations TO maais_web;
        GRANT EXECUTE ON FUNCTION public.maais_append_audit_event(
            uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
        ) TO maais_web;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maais_worker') THEN
        REVOKE ALL ON TABLE public.audit_events, public.health_evaluations FROM maais_worker;
        GRANT SELECT ON TABLE public.audit_events, public.health_evaluations TO maais_worker;
        GRANT EXECUTE ON FUNCTION public.maais_append_audit_event(
            uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
        ) TO maais_worker;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maais_ops') THEN
        REVOKE ALL ON TABLE public.audit_events, public.health_evaluations FROM maais_ops;
        GRANT SELECT ON TABLE public.audit_events, public.health_evaluations TO maais_ops;
        GRANT INSERT ON TABLE public.health_evaluations TO maais_ops;
        GRANT EXECUTE ON FUNCTION public.maais_append_audit_event(
            uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
        ) TO maais_ops;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'maais_verifier') THEN
        REVOKE ALL ON TABLE public.audit_events, public.health_evaluations FROM maais_verifier;
        GRANT SELECT ON TABLE public.audit_events, public.health_evaluations TO maais_verifier;
    END IF;
END
$maais_audit_grants$;
""".strip()
