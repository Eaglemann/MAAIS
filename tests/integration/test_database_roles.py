from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from maais.config.cloud import ServiceRole
from maais.db.replay import verify_ledger_consistency
from maais.db.roles import DatabaseRolePasswords
from maais.db.unit_of_work import UnitOfWork
from maais.operations.migrations import (
    MIGRATION_LOCK_KEY,
    ActiveRunBlocksMaintenance,
    bootstrap_roles_with_url,
    migrate_with_url,
)
from maais.operations.operator_commands import CommandType, OperatorCommand
from maais.platform.identity import RailwayRuntimeIdentity
from tests.integration.test_platform_repository import (
    COMMAND_ONE,
    EXPERIMENT_ONE,
    RUN_ONE,
    WORKER_ONE,
    _descriptor,
    _prepare_activatable_run,
)
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
WEB_BOOT = UUID("99999999-9999-4999-8999-999999999999")


def _passwords() -> DatabaseRolePasswords:
    return DatabaseRolePasswords(
        migrator="migrator-integration-password",  # pragma: allowlist secret
        worker="worker-integration-password",  # pragma: allowlist secret
        web="web-integration-password",  # pragma: allowlist secret
        operations="operations-integration-password",  # pragma: allowlist secret
        verifier="verifier-integration-password",  # pragma: allowlist secret
    )


async def test_role_bootstrap_refuses_an_active_run(
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )
    async with uow_factory.begin() as uow:
        await uow.platform.activate_run(
            RUN_ONE,
            command_id=COMMAND_ONE,
            worker_boot_id=WORKER_ONE,
            started_at=NOW + timedelta(seconds=3),
        )

    with pytest.raises(ActiveRunBlocksMaintenance, match="active run"):
        await bootstrap_roles_with_url(test_database_url, _passwords())


async def test_database_roles_and_security_definer_gateways_enforce_least_privilege(
    db_engine: AsyncEngine,
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    await _assert_roles_absent(db_engine)
    descriptor = _descriptor()
    manifest = _manifest(experiment_id=EXPERIMENT_ONE, schema_revision="0019")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )
    async with db_engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA maais_auth"))
        await connection.execute(
            text("CREATE TABLE maais_auth.operator_sessions (id uuid PRIMARY KEY)")
        )
        await connection.execute(
            text("CREATE TABLE maais_auth.operator_auth_state (id integer PRIMARY KEY)")
        )
        await connection.execute(
            text("CREATE TABLE public.artifact_records (id integer PRIMARY KEY)")
        )
        await connection.execute(
            text("CREATE TABLE public.health_evaluations (id integer PRIMARY KEY)")
        )

    engines: list[AsyncEngine] = []
    try:
        assert await bootstrap_roles_with_url(test_database_url, _passwords()) == (
            "maais_migrator",
            "maais_worker",
            "maais_web",
            "maais_ops",
            "maais_verifier",
        )
        assert await bootstrap_roles_with_url(test_database_url, _passwords()) == (
            "maais_migrator",
            "maais_worker",
            "maais_web",
            "maais_ops",
            "maais_verifier",
        )
        role_engines = {
            ServiceRole.MIGRATOR: _role_engine(
                test_database_url,
                "maais_migrator",
                _passwords().migrator,
            ),
            ServiceRole.WORKER: _role_engine(
                test_database_url,
                "maais_worker",
                _passwords().worker,
            ),
            ServiceRole.WEB: _role_engine(
                test_database_url,
                "maais_web",
                _passwords().web,
            ),
            ServiceRole.OPERATIONS: _role_engine(
                test_database_url,
                "maais_ops",
                _passwords().operations,
            ),
            ServiceRole.VERIFIER: _role_engine(
                test_database_url,
                "maais_verifier",
                _passwords().verifier,
            ),
        }
        engines.extend(role_engines.values())
        web = role_engines[ServiceRole.WEB]
        worker = role_engines[ServiceRole.WORKER]
        operations = role_engines[ServiceRole.OPERATIONS]
        verifier = role_engines[ServiceRole.VERIFIER]
        migrator = role_engines[ServiceRole.MIGRATOR]

        async with web.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM public.experiments")) == 1
            await connection.rollback()
        await _expect_denied(web, "INSERT INTO public.decision_cycles DEFAULT VALUES")
        await _expect_denied(web, "CREATE TABLE public.forbidden_web (id integer)")
        async with web.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO maais_auth.operator_sessions (id) "
                    "VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')"
                )
            )

        command = OperatorCommand.request(
            command_id=COMMAND_ONE,
            experiment_id=EXPERIMENT_ONE,
            command_type=CommandType.START,
            idempotency_key=str(COMMAND_ONE),
            actor="sole_operator",
            reason="approved cloud paper start",
            payload={"run_id": str(RUN_ONE)},
            confirmation="CONFIRM START",
            requested_at=NOW,
        )
        async with web.begin() as connection:
            created_id = await connection.scalar(
                text(
                    "SELECT public.maais_enqueue_operator_command("
                    ":command_id, :experiment_id, :command_type, :idempotency_key, "
                    ":actor, :reason, CAST(:payload AS jsonb), :confirmed, :requested_at)"
                ),
                {
                    "command_id": command.command_id,
                    "experiment_id": command.experiment_id,
                    "command_type": command.command_type.value,
                    "idempotency_key": command.idempotency_key,
                    "actor": command.actor,
                    "reason": command.reason,
                    "payload": json.dumps(dict(command.payload), sort_keys=True),
                    "confirmed": command.operator_confirmed,
                    "requested_at": command.requested_at,
                },
            )
            assert created_id == command.command_id
        async with uow_factory.begin() as uow:
            assert await uow.commands.get(COMMAND_ONE) == command
            assert (await verify_ledger_consistency(uow.session)).ok

        identity = RailwayRuntimeIdentity(
            project_id="project-1",
            environment_id="environment-1",
            service_id="web-service",
            deployment_id="deployment-1",
            snapshot_id=None,
            replica_id="replica-web-1",
            region="europe-west4",
            service_role=ServiceRole.WEB,
            boot_id=WEB_BOOT,
            candidate_hash=descriptor.descriptor_hash,
            started_at=NOW,
        )
        async with web.begin() as connection:
            registered = await connection.scalar(
                text(
                    "SELECT public.maais_register_service_instance("
                    ":boot_id, NULL, :project_id, :environment_id, :service_id, "
                    ":deployment_id, NULL, :replica_id, :region, :service_role, "
                    ":candidate_hash, CAST(:runtime_identity AS jsonb), :started_at, "
                    ":first_seen_at)"
                ),
                {
                    "boot_id": identity.boot_id,
                    "project_id": identity.project_id,
                    "environment_id": identity.environment_id,
                    "service_id": identity.service_id,
                    "deployment_id": identity.deployment_id,
                    "replica_id": identity.replica_id,
                    "region": identity.region,
                    "service_role": identity.service_role.value,
                    "candidate_hash": identity.candidate_hash,
                    "runtime_identity": json.dumps(identity.to_json_data(), sort_keys=True),
                    "started_at": identity.started_at,
                    "first_seen_at": NOW + timedelta(seconds=1),
                },
            )
            assert registered == WEB_BOOT
            heartbeat = await connection.scalar(
                text("SELECT public.maais_heartbeat_service_instance(:boot_id, 1, :heartbeat_at)"),
                {
                    "boot_id": WEB_BOOT,
                    "heartbeat_at": NOW + timedelta(seconds=2),
                },
            )
            assert heartbeat == WEB_BOOT
        await _expect_denied(
            web,
            "SELECT public.maais_register_service_instance("
            ":boot_id, NULL, 'project-1', 'environment-1', 'worker-service', "
            "'deployment-1', NULL, 'replica-worker', 'europe-west4', 'worker', "
            ":candidate_hash, '{}'::jsonb, :started_at, :started_at)",
            {
                "boot_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                "candidate_hash": descriptor.descriptor_hash,
                "started_at": NOW,
            },
        )

        for table_name in ("decision_cycles", "order_intents", "fills"):
            await _expect_denied(
                operations,
                f"INSERT INTO public.{table_name} DEFAULT VALUES",
            )
        async with operations.begin() as connection:
            await connection.execute(text("INSERT INTO public.artifact_records VALUES (1)"))
            await connection.execute(text("INSERT INTO public.health_evaluations VALUES (1)"))
        await _expect_denied(worker, "INSERT INTO public.artifact_records VALUES (2)")
        await _expect_denied(worker, "ALTER TABLE public.experiments ADD COLUMN forbidden int")
        async with verifier.connect() as connection:
            assert await connection.scalar(text("SHOW default_transaction_read_only")) == "on"
            assert await connection.scalar(text("SELECT count(*) FROM public.experiments")) == 1
            await connection.rollback()
        await _expect_denied(
            verifier,
            "INSERT INTO public.health_evaluations VALUES (2)",
            force_read_write=True,
        )

        async with migrator.connect() as first, migrator.connect() as second:
            await first.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": MIGRATION_LOCK_KEY},
            )
            assert (
                await second.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                )
                is False
            )
            assert (
                await first.scalar(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                )
                is True
            )
            await first.commit()
            await second.rollback()
        assert (
            await migrate_with_url(
                _role_url(
                    test_database_url,
                    "maais_migrator",
                    _passwords().migrator,
                ).render_as_string(hide_password=False),
                expected_revision="0019",
                repository_root=Path(__file__).resolve().parents[2],
            )
            == "0019"
        )
    finally:
        for engine in engines:
            await engine.dispose()
        await _cleanup_roles(db_engine)


def _role_url(database_url: str, role_name: str, password: str):
    return make_url(database_url).set(username=role_name, password=password)


def _role_engine(database_url: str, role_name: str, password: str) -> AsyncEngine:
    return create_async_engine(_role_url(database_url, role_name, password), pool_pre_ping=True)


async def _expect_denied(
    engine: AsyncEngine,
    sql: str,
    parameters: dict[str, object] | None = None,
    *,
    force_read_write: bool = False,
) -> None:
    with pytest.raises(DBAPIError) as raised:
        async with engine.begin() as connection:
            if force_read_write:
                await connection.execute(text("SET TRANSACTION READ WRITE"))
            await connection.execute(text(sql), parameters or {})
    assert getattr(raised.value.orig, "sqlstate", None) == "42501"


async def _assert_roles_absent(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        roles = tuple(
            await connection.scalars(
                text(
                    "SELECT rolname FROM pg_roles WHERE rolname IN "
                    "('maais_migrator','maais_worker','maais_web','maais_ops','maais_verifier')"
                )
            )
        )
        await connection.rollback()
    assert roles == ()


async def _cleanup_roles(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "DROP FUNCTION IF EXISTS public.maais_heartbeat_service_instance("
                "uuid, integer, timestamp with time zone)"
            )
        )
        await connection.execute(
            text(
                "DROP FUNCTION IF EXISTS public.maais_register_service_instance("
                "uuid, uuid, text, text, text, text, text, text, text, text, text, jsonb, "
                "timestamp with time zone, timestamp with time zone)"
            )
        )
        await connection.execute(
            text(
                "DROP FUNCTION IF EXISTS public.maais_enqueue_operator_command("
                "uuid, uuid, text, text, text, text, jsonb, boolean, timestamp with time zone)"
            )
        )
        await connection.execute(
            text("DROP FUNCTION IF EXISTS public._maais_canonical_jsonb(jsonb)")
        )
        await connection.execute(
            text("DROP FUNCTION IF EXISTS public._maais_utc_iso(timestamp with time zone)")
        )
        await connection.execute(text("DROP SCHEMA IF EXISTS maais_auth CASCADE"))
        existing = set(
            await connection.scalars(
                text(
                    "SELECT rolname FROM pg_roles WHERE rolname IN "
                    "('maais_migrator','maais_worker','maais_web','maais_ops','maais_verifier')"
                )
            )
        )
        if "maais_migrator" in existing:
            await connection.execute(text("REASSIGN OWNED BY maais_migrator TO maais"))
        await connection.execute(text("DROP TABLE IF EXISTS public.health_evaluations"))
        await connection.execute(text("DROP TABLE IF EXISTS public.artifact_records"))
        for role_name in (
            "maais_worker",
            "maais_web",
            "maais_ops",
            "maais_verifier",
            "maais_migrator",
        ):
            if role_name in existing:
                await connection.execute(text(f"DROP OWNED BY {role_name}"))
                await connection.execute(text(f"DROP ROLE {role_name}"))
        await connection.execute(text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
        await connection.execute(
            text(
                "DO $restore_connect$ BEGIN EXECUTE format("
                "'GRANT CONNECT ON DATABASE %I TO PUBLIC', current_database()); "
                "END $restore_connect$"
            )
        )
