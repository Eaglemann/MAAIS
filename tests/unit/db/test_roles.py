from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from maais.cli import build_parser
from maais.config.cloud import DATABASE_ROLE_BY_SERVICE, ServiceRole
from maais.db.roles import (
    AUTH_DML_TABLES,
    PUBLIC_DML_TABLES_BY_ROLE,
    DatabaseRolePasswords,
    build_role_bootstrap_statements,
    load_database_role_passwords,
)
from maais.operations.migrations import (
    MIGRATION_LOCK_KEY,
    ActiveRunBlocksMaintenance,
    SchemaIdentityError,
    assert_expected_schema,
    ensure_no_active_runs,
)


def _passwords() -> DatabaseRolePasswords:
    return DatabaseRolePasswords(
        migrator="migrator-test-password",  # pragma: allowlist secret
        worker="worker-test-password",  # pragma: allowlist secret
        web="web-test-password",  # pragma: allowlist secret
        operations="operations-test-password",  # pragma: allowlist secret
        verifier="verifier-test-password",  # pragma: allowlist secret
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
    bootstrap = parser.parse_args(["cloud-bootstrap-roles"])
    migrate = parser.parse_args(
        ["cloud-migrate", "--expected-revision", "0019", "--repository", "."]
    )

    assert bootstrap.command == "cloud-bootstrap-roles"
    assert migrate.expected_revision == "0019"
    assert not any("password" in name for name in vars(bootstrap))
    assert not any("password" in name for name in vars(migrate))


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
