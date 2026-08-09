from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import make_url

from maais.cli import build_parser
from maais.config.cloud import DATABASE_ROLE_BY_SERVICE, ServiceRole
from maais.db.roles import (
    AUTH_DML_TABLES,
    PUBLIC_DML_TABLES_BY_ROLE,
    PUBLIC_INSERT_ONLY_TABLES_BY_ROLE,
    DatabaseRolePasswords,
    build_role_bootstrap_statements,
    build_role_principal_statements,
    load_database_role_passwords,
)
from maais.operations.migrations import (
    MIGRATION_LOCK_KEY,
    ActiveRunBlocksMaintenance,
    SchemaIdentityError,
    _database_url_for_role,
    _database_url_with_maintenance_timeouts,
    _upgrade_to_head,
    assert_expected_schema,
    ensure_no_active_runs,
    initialize_database_with_url,
)


def _passwords() -> DatabaseRolePasswords:
    return DatabaseRolePasswords(
        migrator="m" * 32,
        worker="w" * 32,
        web="b" * 32,
        operations="o" * 32,
        verifier="v" * 32,
    )


def test_bootstrap_uses_only_fixed_role_identifiers_and_bound_passwords() -> None:
    passwords = _passwords()
    statements = build_role_bootstrap_statements(passwords)
    rendered = "\n".join(statement.sql for statement in statements)

    assert set(DATABASE_ROLE_BY_SERVICE.values()) == {
        "maais_migrator",
        "maais_worker",
        "maais_web",
        "maais_ops",
        "maais_verifier",
    }
    assert all(password not in rendered for password in passwords.values())
    assert set(
        value
        for statement in statements
        for key, value in statement.parameters.items()
        if key.endswith("_password")
    ) == set(passwords.values())
    assert "IF NOT EXISTS" in rendered
    assert "CREATE OR REPLACE FUNCTION" in rendered
    assert all(password not in repr(passwords) for password in passwords.values())
    assert all(
        password not in repr(statement)
        for statement in statements
        for password in passwords.values()
    )


def test_pre_migration_principals_never_compile_table_dependent_gateways() -> None:
    rendered = "\n".join(
        statement.sql for statement in build_role_principal_statements(_passwords())
    )

    assert "CREATE ROLE maais_migrator" in rendered
    assert "ALTER SCHEMA public OWNER TO maais_migrator" in rendered
    assert "GRANT CREATE ON DATABASE %I TO maais_migrator" in rendered
    assert "REVOKE CREATE ON DATABASE %I FROM PUBLIC" in rendered
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE maais_migrator" in rendered
    assert "CREATE OR REPLACE FUNCTION" not in rendered
    assert "public.service_instances%ROWTYPE" not in rendered


def test_role_password_environment_is_complete_and_never_uses_cli_values() -> None:
    environment = {
        "MAAIS_MIGRATOR_DATABASE_PASSWORD": _passwords().migrator,
        "MAAIS_WORKER_DATABASE_PASSWORD": _passwords().worker,
        "MAAIS_WEB_DATABASE_PASSWORD": _passwords().web,
        "MAAIS_OPERATIONS_DATABASE_PASSWORD": _passwords().operations,
        "MAAIS_VERIFIER_DATABASE_PASSWORD": _passwords().verifier,
    }

    assert load_database_role_passwords(environment).values() == _passwords().values()
    with pytest.raises(ValueError, match="MAAIS_VERIFIER_DATABASE_PASSWORD"):
        load_database_role_passwords(
            {key: value for key, value in environment.items() if "VERIFIER" not in key}
        )


def test_cloud_maintenance_cli_never_accepts_password_arguments() -> None:
    parser = build_parser()
    bootstrap = parser.parse_args(
        ["cloud-bootstrap-roles", "--expected-revision", "0022", "--repository", "."]
    )
    migrate = parser.parse_args(
        ["cloud-migrate", "--expected-revision", "0019", "--repository", "."]
    )

    assert bootstrap.command == "cloud-bootstrap-roles"
    assert bootstrap.expected_revision == "0022"
    assert migrate.expected_revision == "0019"
    assert not any("password" in name for name in vars(bootstrap))
    assert not any("password" in name for name in vars(migrate))


def test_database_role_url_uses_psycopg_and_replaces_administrator_identity() -> None:
    role_url = make_url(
        _database_url_for_role(
            "postgresql://admin@postgres.railway.internal:5432/railway",
            role_name="maais_migrator",
            password=_passwords().migrator,
        )
    )

    assert role_url.drivername == "postgresql+psycopg"
    assert role_url.username == "maais_migrator"
    assert role_url.password == _passwords().migrator
    assert role_url.host == "postgres.railway.internal"
    assert role_url.database == "railway"
    assert "admin@" not in role_url.render_as_string(hide_password=False)


def test_maintenance_database_url_bounds_locks_without_dropping_query_options() -> None:
    bounded = make_url(
        _database_url_with_maintenance_timeouts(
            "postgresql+psycopg://admin@postgres.railway.internal:5432/railway?sslmode=require"
        )
    )

    assert bounded.query["sslmode"] == "require"
    assert "lock_timeout=10000ms" in bounded.query["options"]
    assert "statement_timeout=120000ms" in bounded.query["options"]


def test_existing_object_ownership_skips_objects_already_owned_by_migrator() -> None:
    rendered = "\n".join(
        statement.sql for statement in build_role_principal_statements(_passwords())
    )

    assert "JOIN pg_catalog.pg_roles AS owner" in rendered
    assert "owner.rolname <> 'maais_migrator'" in rendered


def test_upgrade_to_head_reuses_the_connection_that_holds_the_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, path: str) -> None:
            observed["config_path"] = path
            self.attributes: dict[str, object] = {}

        def set_main_option(self, key: str, value: str) -> None:
            observed[key] = value

    def upgrade(config: FakeConfig, target: str) -> None:
        observed["connection"] = config.attributes.get("connection")
        observed["target"] = target

    held_connection = object()
    config_path = Path("/workspace/alembic.ini")
    monkeypatch.setattr("maais.operations.migrations.Config", FakeConfig)
    monkeypatch.setattr("maais.operations.migrations.command.upgrade", upgrade)

    _upgrade_to_head(held_connection, config_path)

    assert observed == {
        "config_path": str(config_path),
        "script_location": "/workspace/alembic",
        "connection": held_connection,
        "target": "head",
    }


def test_alembic_environment_accepts_an_externally_managed_connection() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    source = (repository_root / "alembic" / "env.py").read_text()

    assert 'config.attributes.get("connection")' in source
    assert "do_run_migrations(external_connection)" in source


@pytest.mark.asyncio
async def test_database_initialization_orders_principals_migration_and_final_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    stage_events: list[tuple[str, str, str, str]] = []

    def log_stage(
        event: str,
        *,
        stage: str,
        operation_id: str,
        outcome: str,
        **_: object,
    ) -> None:
        stage_events.append((event, stage, operation_id, outcome))

    async def bootstrap_principals(url: str, passwords: DatabaseRolePasswords) -> None:
        calls.append(("principals", (make_url(url).username, passwords)))

    async def migrate(
        url: str,
        *,
        expected_revision: str,
        repository_root: Path,
    ) -> str:
        parsed = make_url(url)
        calls.append(
            (
                "migration",
                (
                    parsed.drivername,
                    parsed.username,
                    parsed.password,
                    expected_revision,
                    repository_root,
                ),
            )
        )
        return expected_revision

    async def bootstrap_roles(url: str, passwords: DatabaseRolePasswords) -> tuple[str, ...]:
        calls.append(("final-grants", (make_url(url).username, passwords)))
        return ("maais_migrator", "maais_worker")

    monkeypatch.setattr(
        "maais.operations.migrations._bootstrap_principals_with_url",
        bootstrap_principals,
    )
    monkeypatch.setattr("maais.operations.migrations.logger.info", log_stage)
    monkeypatch.setattr("maais.operations.migrations.migrate_with_url", migrate)
    monkeypatch.setattr("maais.operations.migrations.bootstrap_roles_with_url", bootstrap_roles)
    repository_root = Path("/workspace")

    result = await initialize_database_with_url(
        "postgresql://admin@postgres.railway.internal:5432/railway",
        _passwords(),
        expected_revision="0022",
        repository_root=repository_root,
    )

    assert result == ("0022", ("maais_migrator", "maais_worker"))
    assert [name for name, _ in calls] == ["principals", "migration", "final-grants"]
    assert calls[0][1] == ("admin", _passwords())
    assert calls[1][1] == (
        "postgresql+psycopg",
        "maais_migrator",
        _passwords().migrator,
        "0022",
        repository_root,
    )
    assert calls[2][1] == ("admin", _passwords())
    assert stage_events == [
        ("cloud_database_bootstrap_stage", "principals", "bootstrap:principals", "started"),
        ("cloud_database_bootstrap_stage", "principals", "bootstrap:principals", "completed"),
        ("cloud_database_bootstrap_stage", "migration", "bootstrap:migration", "started"),
        ("cloud_database_bootstrap_stage", "migration", "bootstrap:migration", "completed"),
        ("cloud_database_bootstrap_stage", "final_grants", "bootstrap:final_grants", "started"),
        (
            "cloud_database_bootstrap_stage",
            "final_grants",
            "bootstrap:final_grants",
            "completed",
        ),
    ]


def test_runtime_roles_are_explicitly_unprivileged_and_web_has_no_public_dml() -> None:
    rendered = "\n".join(
        statement.sql for statement in build_role_bootstrap_statements(_passwords())
    ).upper()

    for forbidden in (
        " SUPERUSER",
        " CREATEDB",
        " CREATEROLE",
        " REPLICATION",
        " BYPASSRLS",
        "GRANT CREATE ON SCHEMA PUBLIC TO MAAIS_WEB",
        "GRANT CREATE ON SCHEMA PUBLIC TO MAAIS_WORKER",
        "GRANT CREATE ON SCHEMA PUBLIC TO MAAIS_OPS",
        "GRANT CREATE ON SCHEMA PUBLIC TO MAAIS_VERIFIER",
    ):
        assert forbidden not in rendered.replace(" NO" + forbidden[1:], "")
    assert PUBLIC_DML_TABLES_BY_ROLE[ServiceRole.WEB] == ()
    assert "health_evaluations" not in PUBLIC_DML_TABLES_BY_ROLE[ServiceRole.OPERATIONS]
    assert PUBLIC_INSERT_ONLY_TABLES_BY_ROLE[ServiceRole.OPERATIONS] == ("health_evaluations",)
    assert AUTH_DML_TABLES == (
        "operator_sessions",
        "operator_auth_state",
    )
    assert "SELECT, INSERT, UPDATE, DELETE" not in rendered
    assert "SELECT, INSERT, UPDATE ON TABLE MAAIS_AUTH" in rendered
    assert "ALTER ROLE MAAIS_VERIFIER SET DEFAULT_TRANSACTION_READ_ONLY = ON" in rendered


def test_gateway_functions_are_fixed_search_path_and_caller_restricted() -> None:
    function_sql = "\n".join(
        statement.sql
        for statement in build_role_bootstrap_statements(_passwords())
        if "FUNCTION" in statement.sql
    )

    assert "SECURITY DEFINER" in function_sql
    assert "SET search_path = pg_catalog, public" in function_sql
    assert "session_user" in function_sql
    assert "maais_enqueue_operator_command" in function_sql
    assert "maais_register_service_instance" in function_sql
    assert "maais_heartbeat_service_instance" in function_sql
    assert "maais_stop_service_instance" in function_sql
    assert "maais_append_audit_event" in function_sql
    assert "REVOKE ALL" in function_sql
    assert "GRANT EXECUTE" in function_sql
    for role_name in ("maais_migrator", "maais_worker", "maais_web", "maais_ops", "maais_verifier"):
        assert f"WHEN '{role_name}'" in function_sql
    rendered = "\n".join(
        statement.sql for statement in build_role_bootstrap_statements(_passwords())
    )
    for role_name in ("maais_worker", "maais_web", "maais_ops", "maais_verifier"):
        assert f"GRANT SELECT ON TABLE public.alembic_version TO {role_name}" in rendered


@pytest.mark.asyncio
async def test_schema_identity_and_active_run_guards_fail_closed() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=["0019", "run_instances", 0, "0018", "run_instances", 1])

    await assert_expected_schema(session, "0019")
    await ensure_no_active_runs(session)
    with pytest.raises(SchemaIdentityError, match="expected=0019 actual=0018"):
        await assert_expected_schema(session, "0019")
    with pytest.raises(ActiveRunBlocksMaintenance, match="active"):
        await ensure_no_active_runs(session)
    assert MIGRATION_LOCK_KEY == 5_321_109_104_001_922_019
