"""Fixed, least-privilege PostgreSQL role and gateway bootstrap."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from maais.config.cloud import DATABASE_ROLE_BY_SERVICE, ServiceRole


@dataclass(frozen=True, slots=True)
class BoundSQL:
    sql: str
    parameters: Mapping[str, object] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True, repr=False)
class DatabaseRolePasswords:
    migrator: str
    worker: str
    web: str
    operations: str
    verifier: str

    def __post_init__(self) -> None:
        for name, value in zip(
            ("migrator", "worker", "web", "operations", "verifier"),
            self.values(),
            strict=True,
        ):
            if not isinstance(value, str) or value != value.strip() or not 16 <= len(value) <= 512:
                raise ValueError(f"{name} database password must be 16-512 trimmed characters")

    def values(self) -> tuple[str, ...]:
        return (
            self.migrator,
            self.worker,
            self.web,
            self.operations,
            self.verifier,
        )

    def by_service(self) -> Mapping[ServiceRole, str]:
        return MappingProxyType(
            {
                ServiceRole.MIGRATOR: self.migrator,
                ServiceRole.WORKER: self.worker,
                ServiceRole.WEB: self.web,
                ServiceRole.OPERATIONS: self.operations,
                ServiceRole.VERIFIER: self.verifier,
            }
        )


ROLE_PASSWORD_ENV: Final[Mapping[ServiceRole, str]] = MappingProxyType(
    {
        ServiceRole.MIGRATOR: "MAAIS_MIGRATOR_DATABASE_PASSWORD",
        ServiceRole.WORKER: "MAAIS_WORKER_DATABASE_PASSWORD",
        ServiceRole.WEB: "MAAIS_WEB_DATABASE_PASSWORD",
        ServiceRole.OPERATIONS: "MAAIS_OPERATIONS_DATABASE_PASSWORD",
        ServiceRole.VERIFIER: "MAAIS_VERIFIER_DATABASE_PASSWORD",
    }
)


def load_database_role_passwords(environment: Mapping[str, str]) -> DatabaseRolePasswords:
    values = {
        service_role: environment.get(variable_name, "")
        for service_role, variable_name in ROLE_PASSWORD_ENV.items()
    }
    missing = [
        ROLE_PASSWORD_ENV[service_role] for service_role, value in values.items() if not value
    ]
    if missing:
        raise ValueError(
            "database role bootstrap is missing secret variables: " + ", ".join(missing)
        )
    return DatabaseRolePasswords(
        migrator=values[ServiceRole.MIGRATOR],
        worker=values[ServiceRole.WORKER],
        web=values[ServiceRole.WEB],
        operations=values[ServiceRole.OPERATIONS],
        verifier=values[ServiceRole.VERIFIER],
    )


PUBLIC_TABLES: Final[tuple[str, ...]] = (
    "account_snapshots",
    "alembic_version",
    "agent_evaluations",
    "agent_versions",
    "agent_weights",
    "counterfactuals",
    "data_quality_evaluations",
    "decision_cycles",
    "decision_summaries",
    "domain_events",
    "event_streams",
    "execution_sensitivities",
    "exit_plans",
    "experiments",
    "fills",
    "funding_entries",
    "funding_rates",
    "gate_evaluations",
    "incidents",
    "klines",
    "market_cursors",
    "market_frames",
    "market_recovery_runs",
    "operator_commands",
    "order_book_snapshots",
    "order_events",
    "order_intents",
    "outbox_events",
    "platform_candidates",
    "position_lots",
    "positions",
    "post_trade_reasoning",
    "run_instances",
    "service_instances",
    "strategy_versions",
    "trade_lots",
    "trade_proposals",
    "trade_records",
    "trading_controls",
    "worker_checkpoints",
    "worker_leases",
    # Added by the sequential cloud migrations and granted only after they exist.
    "artifact_publication_attempts",
    "artifact_records",
    "scheduled_operations",
    "audit_events",
    "health_evaluations",
)

_WORKER_DML: Final[tuple[str, ...]] = (
    "account_snapshots",
    "agent_evaluations",
    "agent_versions",
    "agent_weights",
    "counterfactuals",
    "data_quality_evaluations",
    "decision_cycles",
    "decision_summaries",
    "domain_events",
    "event_streams",
    "execution_sensitivities",
    "exit_plans",
    "experiments",
    "fills",
    "funding_entries",
    "funding_rates",
    "gate_evaluations",
    "incidents",
    "klines",
    "market_cursors",
    "market_frames",
    "market_recovery_runs",
    "operator_commands",
    "order_book_snapshots",
    "order_events",
    "order_intents",
    "outbox_events",
    "position_lots",
    "positions",
    "post_trade_reasoning",
    "run_instances",
    "strategy_versions",
    "trade_lots",
    "trade_proposals",
    "trade_records",
    "trading_controls",
    "worker_checkpoints",
    "worker_leases",
)

_OPERATIONS_DML: Final[tuple[str, ...]] = (
    "domain_events",
    "event_streams",
    "platform_candidates",
    "run_instances",
    "incidents",
    "outbox_events",
    "artifact_publication_attempts",
    "artifact_records",
    "scheduled_operations",
)

_OPERATIONS_INSERT_ONLY: Final[tuple[str, ...]] = ("health_evaluations",)

PUBLIC_DML_TABLES_BY_ROLE: Final[Mapping[ServiceRole, tuple[str, ...]]] = MappingProxyType(
    {
        ServiceRole.WEB: (),
        ServiceRole.WORKER: _WORKER_DML,
        ServiceRole.OPERATIONS: _OPERATIONS_DML,
        ServiceRole.VERIFIER: (),
        ServiceRole.MIGRATOR: (),
    }
)

PUBLIC_INSERT_ONLY_TABLES_BY_ROLE: Final[Mapping[ServiceRole, tuple[str, ...]]] = MappingProxyType(
    {
        ServiceRole.WEB: (),
        ServiceRole.WORKER: (),
        ServiceRole.OPERATIONS: _OPERATIONS_INSERT_ONLY,
        ServiceRole.VERIFIER: (),
        ServiceRole.MIGRATOR: (),
    }
)

AUTH_DML_TABLES: Final[tuple[str, ...]] = (
    "operator_sessions",
    "operator_auth_state",
)

PUBLIC_SEQUENCES: Final[tuple[str, ...]] = (
    "agent_weights_id_seq",
    "domain_events_global_position_seq",
    "funding_rates_id_seq",
    "klines_id_seq",
    "order_book_snapshots_id_seq",
    "outbox_events_cursor_seq",
    "post_trade_reasoning_id_seq",
)

_RUNTIME_ROLES: Final[tuple[str, ...]] = (
    "maais_worker",
    "maais_web",
    "maais_ops",
    "maais_verifier",
)


def build_role_bootstrap_statements(passwords: DatabaseRolePasswords) -> tuple[BoundSQL, ...]:
    statements: list[BoundSQL] = []
    for service_role, password in passwords.by_service().items():
        role_name = DATABASE_ROLE_BY_SERVICE[service_role]
        parameter_name = f"{service_role.value}_password"
        setting_name = f"maais.bootstrap_{service_role.value}_password"
        statements.append(
            BoundSQL(
                f"SELECT pg_catalog.set_config('{setting_name}', :{parameter_name}, true)",
                {parameter_name: password},
            )
        )
        statements.append(BoundSQL(_role_statement(role_name, setting_name), {}))

    statements.extend(
        (
            BoundSQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC", {}),
            BoundSQL("REVOKE USAGE ON SCHEMA public FROM PUBLIC", {}),
            BoundSQL("ALTER SCHEMA public OWNER TO maais_migrator", {}),
            BoundSQL(_OWN_EXISTING_OBJECTS_SQL, {}),
            BoundSQL(_DATABASE_CONNECT_SQL, {}),
            BoundSQL(
                "GRANT USAGE ON SCHEMA public TO " + ", ".join(_RUNTIME_ROLES),
                {},
            ),
            BoundSQL("GRANT CREATE, USAGE ON SCHEMA public TO maais_migrator", {}),
            BoundSQL(_REVOKE_RUNTIME_SQL, {}),
        )
    )
    for role_name in _RUNTIME_ROLES:
        for table_name in PUBLIC_TABLES:
            statements.append(
                BoundSQL(_conditional_table_grant(table_name, "SELECT", role_name), {})
            )
    for service_role, table_names in PUBLIC_DML_TABLES_BY_ROLE.items():
        if service_role in (ServiceRole.MIGRATOR, ServiceRole.WEB, ServiceRole.VERIFIER):
            continue
        role_name = DATABASE_ROLE_BY_SERVICE[service_role]
        for table_name in table_names:
            statements.append(
                BoundSQL(
                    _conditional_table_grant(table_name, "INSERT, UPDATE", role_name),
                    {},
                )
            )
    for service_role, table_names in PUBLIC_INSERT_ONLY_TABLES_BY_ROLE.items():
        if service_role in (ServiceRole.MIGRATOR, ServiceRole.WEB, ServiceRole.VERIFIER):
            continue
        role_name = DATABASE_ROLE_BY_SERVICE[service_role]
        for table_name in table_names:
            statements.append(
                BoundSQL(_conditional_table_grant(table_name, "INSERT", role_name), {})
            )
    for sequence_name in PUBLIC_SEQUENCES:
        for role_name in ("maais_worker", "maais_ops"):
            statements.append(
                BoundSQL(
                    _conditional_sequence_grant(sequence_name, role_name),
                    {},
                )
            )
    statements.extend(
        (
            BoundSQL(_AUTH_GRANTS_SQL, {}),
            BoundSQL(_DEFAULT_PRIVILEGES_SQL, {}),
            BoundSQL(_UTC_ISO_FUNCTION_SQL, {}),
            BoundSQL(_CANONICAL_JSON_FUNCTION_SQL, {}),
            BoundSQL(_COMMAND_GATEWAY_SQL, {}),
            BoundSQL(_REGISTER_SERVICE_GATEWAY_SQL, {}),
            BoundSQL(_HEARTBEAT_SERVICE_GATEWAY_SQL, {}),
            BoundSQL(_FUNCTION_OWNERS_AND_GRANTS_SQL, {}),
            BoundSQL(_AUDIT_FUNCTION_GRANTS_SQL, {}),
            BoundSQL(
                "ALTER ROLE maais_verifier SET default_transaction_read_only = on",
                {},
            ),
        )
    )
    return tuple(statements)


async def bootstrap_database_roles(
    connection: AsyncConnection,
    passwords: DatabaseRolePasswords,
) -> None:
    for statement in build_role_bootstrap_statements(passwords):
        await connection.execute(text(statement.sql), dict(statement.parameters))


def _role_statement(role_name: str, setting_name: str) -> str:
    if role_name not in set(DATABASE_ROLE_BY_SERVICE.values()):  # pragma: no cover - constant guard
        raise ValueError("database role identifier is not approved")
    return f"""
DO $maais_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = '{role_name}') THEN
        CREATE ROLE {role_name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    ELSE
        ALTER ROLE {role_name} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    EXECUTE pg_catalog.format(
        'ALTER ROLE {role_name} PASSWORD %L',
        pg_catalog.current_setting('{setting_name}')
    );
END
$maais_role$;
""".strip()


def _conditional_table_grant(table_name: str, privileges: str, role_name: str) -> str:
    return f"""
DO $maais_grant$
BEGIN
    IF pg_catalog.to_regclass('public.{table_name}') IS NOT NULL THEN
        GRANT {privileges} ON TABLE public.{table_name} TO {role_name};
    END IF;
END
$maais_grant$;
""".strip()


def _conditional_sequence_grant(sequence_name: str, role_name: str) -> str:
    return f"""
DO $maais_grant$
BEGIN
    IF pg_catalog.to_regclass('public.{sequence_name}') IS NOT NULL THEN
        GRANT USAGE, SELECT ON SEQUENCE public.{sequence_name} TO {role_name};
    END IF;
END
$maais_grant$;
""".strip()


_OWN_EXISTING_OBJECTS_SQL = """
DO $maais_owner$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT c.relkind, n.nspname, c.relname
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER %s %I.%I OWNER TO maais_migrator',
            CASE item.relkind
                WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW'
                ELSE 'TABLE'
            END,
            item.nspname,
            item.relname
        );
    END LOOP;
END
$maais_owner$;
""".strip()

_DATABASE_CONNECT_SQL = """
DO $maais_database$
BEGIN
    EXECUTE pg_catalog.format(
        'REVOKE CONNECT ON DATABASE %I FROM PUBLIC',
        pg_catalog.current_database()
    );
    EXECUTE pg_catalog.format(
        'GRANT CONNECT ON DATABASE %I TO maais_migrator, maais_worker, ' ||
        'maais_web, maais_ops, maais_verifier',
        pg_catalog.current_database()
    );
END
$maais_database$;
""".strip()

_REVOKE_RUNTIME_SQL = """
REVOKE ALL ON ALL TABLES IN SCHEMA public
    FROM maais_worker, maais_web, maais_ops, maais_verifier;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
    FROM maais_worker, maais_web, maais_ops, maais_verifier;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public
    FROM maais_worker, maais_web, maais_ops, maais_verifier;
""".strip()

_AUTH_GRANTS_SQL = """
DO $maais_auth_grants$
DECLARE
    table_name text;
BEGIN
    IF pg_catalog.to_regnamespace('maais_auth') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA maais_auth TO maais_web;
        FOREACH table_name IN ARRAY ARRAY['operator_sessions', 'operator_auth_state'] LOOP
            IF pg_catalog.to_regclass('maais_auth.' || table_name) IS NOT NULL THEN
                EXECUTE pg_catalog.format(
                    'GRANT SELECT, INSERT, UPDATE ON TABLE maais_auth.%I TO maais_web',
                    table_name
                );
            END IF;
        END LOOP;
    END IF;
END
$maais_auth_grants$;
""".strip()

_DEFAULT_PRIVILEGES_SQL = """
ALTER DEFAULT PRIVILEGES FOR ROLE maais_migrator IN SCHEMA public
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE maais_migrator IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE maais_migrator IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM PUBLIC;
""".strip()

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

_COMMAND_GATEWAY_SQL = """
CREATE OR REPLACE FUNCTION public.maais_enqueue_operator_command(
    p_command_id uuid,
    p_experiment_id uuid,
    p_command_type text,
    p_idempotency_key text,
    p_actor text,
    p_reason text,
    p_payload jsonb,
    p_operator_confirmed boolean,
    p_requested_at timestamp with time zone
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $maais_command$
DECLARE
    existing_id uuid;
    existing_hash text;
    request_state jsonb;
    command_state jsonb;
    request_hash text;
    command_hash text;
    stream_id uuid := pg_catalog.gen_random_uuid();
    event_id uuid := pg_catalog.gen_random_uuid();
    event_position bigint;
    event_metadata jsonb := pg_catalog.jsonb_build_object('schema_revision', '0017');
BEGIN
    IF session_user <> 'maais_web' THEN
        RAISE EXCEPTION 'operator command gateway caller is not authorized'
            USING ERRCODE = '42501';
    END IF;
    IF p_command_id IS NULL OR p_command_id = '00000000-0000-0000-0000-000000000000'::uuid
        OR p_experiment_id IS NULL
        OR p_experiment_id = '00000000-0000-0000-0000-000000000000'::uuid THEN
        RAISE EXCEPTION 'operator command identifiers are invalid' USING ERRCODE = '22023';
    END IF;
    IF p_command_type NOT IN (
        'start', 'pause', 'resume', 'stop', 'emergency_halt', 'flatten',
        'acknowledge_incident', 'resolve_incident', 'reset_kill_switch'
    ) THEN
        RAISE EXCEPTION 'operator command type is invalid' USING ERRCODE = '22023';
    END IF;
    IF p_idempotency_key IS NULL OR p_idempotency_key <> pg_catalog.btrim(p_idempotency_key)
        OR pg_catalog.char_length(p_idempotency_key) NOT BETWEEN 8 AND 128
        OR p_actor IS NULL OR p_actor = '' OR p_actor <> pg_catalog.btrim(p_actor)
        OR p_reason IS NULL OR p_reason = '' OR p_reason <> pg_catalog.btrim(p_reason)
        OR p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) <> 'object'
        OR p_operator_confirmed IS NULL
        OR p_requested_at IS NULL THEN
        RAISE EXCEPTION 'operator command arguments are invalid' USING ERRCODE = '22023';
    END IF;
    IF p_command_type IN (
        'start', 'pause', 'resume', 'stop', 'emergency_halt', 'flatten',
        'resolve_incident', 'reset_kill_switch'
    ) AND NOT p_operator_confirmed THEN
        RAISE EXCEPTION 'operator confirmation is required' USING ERRCODE = '22023';
    END IF;

    request_state := pg_catalog.jsonb_build_object(
        'actor', p_actor,
        'command_type', p_command_type,
        'experiment_id', p_experiment_id::text,
        'idempotency_key', p_idempotency_key,
        'operator_confirmed', p_operator_confirmed,
        'payload', p_payload,
        'reason', p_reason
    );
    request_hash := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(public._maais_canonical_jsonb(request_state), 'UTF8')
        ),
        'hex'
    );
    command_state := pg_catalog.jsonb_build_object(
        'accepted_at', NULL,
        'accepted_by', NULL,
        'actor', p_actor,
        'command_id', p_command_id::text,
        'command_type', p_command_type,
        'completed_at', NULL,
        'experiment_id', p_experiment_id::text,
        'idempotency_key', p_idempotency_key,
        'operator_confirmed', p_operator_confirmed,
        'payload', p_payload,
        'reason', p_reason,
        'request_hash', request_hash,
        'requested_at', public._maais_utc_iso(p_requested_at),
        'result', NULL,
        'status', 'requested',
        'version', 1
    );
    command_hash := pg_catalog.encode(
        pg_catalog.sha256(
            pg_catalog.convert_to(public._maais_canonical_jsonb(command_state), 'UTF8')
        ),
        'hex'
    );

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_experiment_id::text || ':' || p_idempotency_key, 19017)
    );
    SELECT id, operator_commands.request_hash
    INTO existing_id, existing_hash
    FROM public.operator_commands
    WHERE experiment_id = p_experiment_id AND idempotency_key = p_idempotency_key
    FOR UPDATE;
    IF existing_id IS NOT NULL THEN
        IF existing_hash <> request_hash THEN
            RAISE EXCEPTION 'operator command idempotency identity conflicts'
                USING ERRCODE = '23505';
        END IF;
        RETURN existing_id;
    END IF;
    IF EXISTS (SELECT 1 FROM public.operator_commands WHERE id = p_command_id) THEN
        RAISE EXCEPTION 'operator command identifier conflicts' USING ERRCODE = '23505';
    END IF;

    INSERT INTO public.operator_commands (
        id, experiment_id, command_type, status, idempotency_key, actor, reason,
        payload_json, operator_confirmed, request_hash, requested_at, version,
        accepted_at, accepted_by, completed_at, result_json, content_hash
    ) VALUES (
        p_command_id, p_experiment_id, p_command_type, 'requested', p_idempotency_key,
        p_actor, p_reason, p_payload, p_operator_confirmed, request_hash, p_requested_at, 1,
        NULL, NULL, NULL, NULL, command_hash
    );
    INSERT INTO public.event_streams (
        id, aggregate_id, aggregate_type, current_version
    ) VALUES (stream_id, p_command_id, 'operator_command', 1);
    INSERT INTO public.domain_events (
        id, stream_id, aggregate_id, aggregate_type, stream_version, event_type,
        event_version, payload_json, metadata_json, occurred_at
    ) VALUES (
        event_id, stream_id, p_command_id, 'operator_command', 1,
        'operator_command.requested', 1, command_state, event_metadata, p_requested_at
    ) RETURNING global_position INTO event_position;
    INSERT INTO public.outbox_events (
        id, domain_event_id, topic, payload_json, publish_attempts
    ) VALUES (
        pg_catalog.gen_random_uuid(),
        event_id,
        'operator_command.requested',
        pg_catalog.jsonb_build_object(
            'aggregate_id', p_command_id::text,
            'aggregate_type', 'operator_command',
            'event_id', event_id::text,
            'event_type', 'operator_command.requested',
            'event_version', 1,
            'global_position', event_position,
            'metadata', event_metadata,
            'occurred_at', public._maais_utc_iso(p_requested_at),
            'payload', command_state,
            'stream_version', 1
        ),
        0
    );
    RETURN p_command_id;
END
$maais_command$;
""".strip()

_REGISTER_SERVICE_GATEWAY_SQL = """
CREATE OR REPLACE FUNCTION public.maais_register_service_instance(
    p_boot_id uuid,
    p_run_id uuid,
    p_project_id text,
    p_environment_id text,
    p_service_id text,
    p_deployment_id text,
    p_snapshot_id text,
    p_replica_id text,
    p_region text,
    p_service_role text,
    p_candidate_hash text,
    p_runtime_identity jsonb,
    p_started_at timestamp with time zone,
    p_first_seen_at timestamp with time zone
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $maais_service$
DECLARE
    expected_role text;
    existing public.service_instances%ROWTYPE;
BEGIN
    expected_role := CASE session_user
        WHEN 'maais_migrator' THEN 'migrator'
        WHEN 'maais_web' THEN 'web'
        WHEN 'maais_worker' THEN 'worker'
        WHEN 'maais_ops' THEN 'operations'
        WHEN 'maais_verifier' THEN 'verifier'
        ELSE NULL
    END;
    IF expected_role IS NULL OR p_service_role <> expected_role THEN
        RAISE EXCEPTION 'service registration caller role is invalid' USING ERRCODE = '42501';
    END IF;
    IF p_boot_id IS NULL OR p_boot_id = '00000000-0000-0000-0000-000000000000'::uuid
        OR p_project_id IS NULL OR p_project_id = ''
        OR p_project_id <> pg_catalog.btrim(p_project_id)
        OR p_environment_id IS NULL OR p_environment_id = ''
        OR p_environment_id <> pg_catalog.btrim(p_environment_id)
        OR p_service_id IS NULL OR p_service_id = ''
        OR p_service_id <> pg_catalog.btrim(p_service_id)
        OR p_deployment_id IS NULL OR p_deployment_id = ''
        OR p_deployment_id <> pg_catalog.btrim(p_deployment_id)
        OR p_snapshot_id IS NOT NULL AND (
            p_snapshot_id = '' OR p_snapshot_id <> pg_catalog.btrim(p_snapshot_id)
        )
        OR p_replica_id IS NULL OR p_replica_id = ''
        OR p_replica_id <> pg_catalog.btrim(p_replica_id)
        OR p_region IS NULL OR p_region = '' OR p_region <> pg_catalog.btrim(p_region)
        OR p_candidate_hash !~ '^[0-9a-f]{64}$'
        OR p_started_at IS NULL OR p_first_seen_at IS NULL OR p_first_seen_at < p_started_at
        OR pg_catalog.jsonb_typeof(p_runtime_identity) <> 'object'
        OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(p_runtime_identity)) <> 11
        OR p_runtime_identity->>'project_id' <> p_project_id
        OR p_runtime_identity->>'environment_id' <> p_environment_id
        OR p_runtime_identity->>'service_id' <> p_service_id
        OR p_runtime_identity->>'deployment_id' <> p_deployment_id
        OR p_runtime_identity->>'replica_id' <> p_replica_id
        OR p_runtime_identity->>'region' <> p_region
        OR p_runtime_identity->>'service_role' <> p_service_role
        OR p_runtime_identity->>'boot_id' <> p_boot_id::text
        OR p_runtime_identity->>'candidate_hash' <> p_candidate_hash
        OR p_runtime_identity->>'started_at' <> public._maais_utc_iso(p_started_at)
        OR NOT p_runtime_identity ? 'snapshot_id'
        OR (p_snapshot_id IS NULL) <> (p_runtime_identity->'snapshot_id' = 'null'::jsonb)
        OR (p_snapshot_id IS NOT NULL AND p_runtime_identity->>'snapshot_id' <> p_snapshot_id) THEN
        RAISE EXCEPTION 'service registration identity is invalid' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.run_instances
        WHERE id = p_run_id AND (
            candidate_hash <> p_candidate_hash OR railway_environment_id <> p_environment_id
        )
    ) THEN
        RAISE EXCEPTION 'service identity does not match run' USING ERRCODE = '22023';
    END IF;

    INSERT INTO public.service_instances (
        boot_id, run_id, project_id, environment_id, service_id, deployment_id,
        snapshot_id, replica_id, region, service_role, candidate_hash,
        runtime_identity_json, started_at, first_seen_at, last_heartbeat_at,
        heartbeat_sequence, stopped_at, terminal_reason
    ) VALUES (
        p_boot_id, p_run_id, p_project_id, p_environment_id, p_service_id,
        p_deployment_id, p_snapshot_id, p_replica_id, p_region, p_service_role,
        p_candidate_hash, p_runtime_identity, p_started_at, p_first_seen_at,
        p_first_seen_at, 0, NULL, NULL
    ) ON CONFLICT (boot_id) DO NOTHING;
    SELECT * INTO existing FROM public.service_instances WHERE boot_id = p_boot_id FOR UPDATE;
    IF existing.boot_id IS NULL
        OR existing.run_id IS DISTINCT FROM p_run_id
        OR existing.project_id <> p_project_id
        OR existing.environment_id <> p_environment_id
        OR existing.service_id <> p_service_id
        OR existing.deployment_id <> p_deployment_id
        OR existing.snapshot_id IS DISTINCT FROM p_snapshot_id
        OR existing.replica_id <> p_replica_id
        OR existing.region <> p_region
        OR existing.service_role <> p_service_role
        OR existing.candidate_hash <> p_candidate_hash
        OR existing.runtime_identity_json <> p_runtime_identity
        OR existing.started_at <> p_started_at
        OR existing.first_seen_at <> p_first_seen_at THEN
        RAISE EXCEPTION 'service boot identity conflicts' USING ERRCODE = '23505';
    END IF;
    RETURN p_boot_id;
END
$maais_service$;
""".strip()

_HEARTBEAT_SERVICE_GATEWAY_SQL = """
CREATE OR REPLACE FUNCTION public.maais_heartbeat_service_instance(
    p_boot_id uuid,
    p_sequence integer,
    p_heartbeat_at timestamp with time zone
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $maais_heartbeat$
DECLARE
    expected_role text;
    existing public.service_instances%ROWTYPE;
BEGIN
    expected_role := CASE session_user
        WHEN 'maais_migrator' THEN 'migrator'
        WHEN 'maais_web' THEN 'web'
        WHEN 'maais_worker' THEN 'worker'
        WHEN 'maais_ops' THEN 'operations'
        WHEN 'maais_verifier' THEN 'verifier'
        ELSE NULL
    END;
    SELECT * INTO existing FROM public.service_instances WHERE boot_id = p_boot_id FOR UPDATE;
    IF existing.boot_id IS NULL THEN
        RAISE EXCEPTION 'service boot does not exist' USING ERRCODE = 'P0002';
    END IF;
    IF expected_role IS NULL OR existing.service_role <> expected_role THEN
        RAISE EXCEPTION 'service heartbeat caller role is invalid' USING ERRCODE = '42501';
    END IF;
    IF existing.stopped_at IS NOT NULL THEN
        RAISE EXCEPTION 'stopped service cannot heartbeat' USING ERRCODE = '55000';
    END IF;
    IF p_sequence IS NULL OR p_heartbeat_at IS NULL THEN
        RAISE EXCEPTION 'service heartbeat arguments are invalid' USING ERRCODE = '22023';
    END IF;
    IF p_sequence = existing.heartbeat_sequence
        AND p_heartbeat_at = existing.last_heartbeat_at THEN
        RETURN p_boot_id;
    END IF;
    IF p_sequence <= existing.heartbeat_sequence
        OR p_heartbeat_at < existing.last_heartbeat_at THEN
        RAISE EXCEPTION 'service heartbeat regressed' USING ERRCODE = '22023';
    END IF;
    UPDATE public.service_instances
    SET heartbeat_sequence = p_sequence, last_heartbeat_at = p_heartbeat_at
    WHERE boot_id = p_boot_id;
    RETURN p_boot_id;
END
$maais_heartbeat$;
""".strip()

_FUNCTION_OWNERS_AND_GRANTS_SQL = """
ALTER FUNCTION public._maais_utc_iso(timestamp with time zone) OWNER TO maais_migrator;
ALTER FUNCTION public._maais_canonical_jsonb(jsonb) OWNER TO maais_migrator;
ALTER FUNCTION public.maais_enqueue_operator_command(
    uuid, uuid, text, text, text, text, jsonb, boolean, timestamp with time zone
) OWNER TO maais_migrator;
ALTER FUNCTION public.maais_register_service_instance(
    uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb,
    timestamp with time zone, timestamp with time zone
) OWNER TO maais_migrator;
ALTER FUNCTION public.maais_heartbeat_service_instance(
    uuid, integer, timestamp with time zone
) OWNER TO maais_migrator;
REVOKE ALL ON FUNCTION public._maais_utc_iso(timestamp with time zone) FROM PUBLIC;
REVOKE ALL ON FUNCTION public._maais_canonical_jsonb(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maais_enqueue_operator_command(
    uuid, uuid, text, text, text, text, jsonb, boolean, timestamp with time zone
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maais_register_service_instance(
    uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb,
    timestamp with time zone, timestamp with time zone
) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.maais_heartbeat_service_instance(
    uuid, integer, timestamp with time zone
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.maais_enqueue_operator_command(
    uuid, uuid, text, text, text, text, jsonb, boolean, timestamp with time zone
) TO maais_web;
GRANT EXECUTE ON FUNCTION public.maais_register_service_instance(
    uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb,
    timestamp with time zone, timestamp with time zone
) TO maais_migrator, maais_worker, maais_web, maais_ops, maais_verifier;
GRANT EXECUTE ON FUNCTION public.maais_heartbeat_service_instance(
    uuid, integer, timestamp with time zone
) TO maais_migrator, maais_worker, maais_web, maais_ops, maais_verifier;
""".strip()

_AUDIT_FUNCTION_GRANTS_SQL = """
DO $maais_audit_grants$
BEGIN
    IF pg_catalog.to_regprocedure(
        'public.maais_append_audit_event(uuid,text,text,text,text,jsonb,uuid,uuid,timestamptz)'
    ) IS NOT NULL THEN
        ALTER FUNCTION public.maais_append_audit_event(
            uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
        ) OWNER TO maais_migrator;
        REVOKE ALL ON FUNCTION public.maais_append_audit_event(
            uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.maais_append_audit_event(
            uuid, text, text, text, text, jsonb, uuid, uuid, timestamp with time zone
        ) TO maais_migrator, maais_worker, maais_web, maais_ops;
    END IF;
    IF pg_catalog.to_regprocedure('public._maais_audit_evidence_safe(jsonb)') IS NOT NULL THEN
        ALTER FUNCTION public._maais_audit_evidence_safe(jsonb) OWNER TO maais_migrator;
        REVOKE ALL ON FUNCTION public._maais_audit_evidence_safe(jsonb) FROM PUBLIC;
    END IF;
    IF pg_catalog.to_regprocedure(
        'public._maais_reject_immutable_evidence_mutation()'
    ) IS NOT NULL THEN
        ALTER FUNCTION public._maais_reject_immutable_evidence_mutation()
            OWNER TO maais_migrator;
        REVOKE ALL ON FUNCTION public._maais_reject_immutable_evidence_mutation()
            FROM PUBLIC;
    END IF;
END
$maais_audit_grants$;
""".strip()
