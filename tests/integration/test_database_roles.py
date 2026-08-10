from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from maais.config.cloud import ServiceRole
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.operator_commands import OperatorCommandConflict
from maais.db.unit_of_work import UnitOfWork
from maais.observability.audit import pseudonymous_reference
from maais.operations.migrations import (
    MIGRATION_LOCK_KEY,
    ActiveRunBlocksMaintenance,
    bootstrap_roles_with_url,
    initialize_database_with_url,
    migrate_with_url,
)
from maais.operations.operator_commands import CommandType, OperatorCommand
from maais.platform.identity import RailwayRuntimeIdentity
from tests.integration.database_role_support import (
    cleanup_database_roles as _cleanup_roles,
)
from tests.integration.database_role_support import (
    integration_role_passwords as _passwords,
)
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
WEB_AUDIT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
WORKER_AUDIT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")
OPERATIONS_AUDIT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3")
EMPTY_BOOTSTRAP_DATABASE = "maais_empty_bootstrap_test"


async def test_empty_database_bootstrap_reaches_the_expected_schema(
    db_engine: AsyncEngine,
    test_database_url: str,
) -> None:
    await _cleanup_roles(db_engine)
    await _replace_test_database(db_engine, EMPTY_BOOTSTRAP_DATABASE)
    empty_database_url = make_url(test_database_url).set(database=EMPTY_BOOTSTRAP_DATABASE)
    migrated_engine: AsyncEngine | None = None
    try:
        revision, roles = await initialize_database_with_url(
            empty_database_url.render_as_string(hide_password=False),
            _passwords(),
            expected_revision="0022",
            repository_root=Path(__file__).resolve().parents[2],
        )

        assert revision == "0022"
        assert roles == (
            "maais_migrator",
            "maais_worker",
            "maais_web",
            "maais_ops",
            "maais_verifier",
        )
        migrated_engine = _role_engine(
            empty_database_url.render_as_string(hide_password=False),
            "maais_migrator",
            _passwords().migrator,
        )
        async with migrated_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0022"
            )
            assert await connection.scalar(text("SELECT to_regclass('public.audit_events')"))
            assert await connection.scalar(
                text("SELECT to_regclass('maais_auth.operator_sessions')")
            )
            await connection.rollback()
    finally:
        if migrated_engine is not None:
            await migrated_engine.dispose()
        await _drop_test_database(db_engine, EMPTY_BOOTSTRAP_DATABASE)
        await _cleanup_roles(db_engine)


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
    manifest = _manifest(experiment_id=EXPERIMENT_ONE, schema_revision="0022")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
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

        async with migrator.begin() as connection:
            await connection.execute(
                text("CREATE SCHEMA maais_migration_probe AUTHORIZATION maais_migrator")
            )
            await connection.execute(text("DROP SCHEMA maais_migration_probe"))
        await _expect_denied(worker, "CREATE SCHEMA maais_runtime_probe")

        async with web.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM public.experiments")) == 1
            await connection.rollback()
        await _expect_denied(web, "INSERT INTO public.decision_cycles DEFAULT VALUES")
        await _expect_denied(web, "CREATE TABLE public.forbidden_web (id integer)")
        async with web.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO maais_auth.operator_sessions "
                    "(id, token_hash, csrf_hash, actor, created_at, last_seen_at, "
                    "expires_at, revoked_at, version) VALUES "
                    "('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', :token_hash, :csrf_hash, "
                    "'sole_operator', :now, :now, :expires_at, NULL, 1)"
                ),
                {
                    "token_hash": "a" * 64,
                    "csrf_hash": "b" * 64,
                    "now": NOW,
                    "expires_at": NOW + timedelta(hours=12),
                },
            )
        await _expect_denied(web, "DELETE FROM maais_auth.operator_sessions")

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

        gateway_uow = UnitOfWork(async_sessionmaker(web, expire_on_commit=False))
        gateway_command = OperatorCommand.request(
            command_id=UUID("55555555-5555-4555-8555-555555555556"),
            experiment_id=EXPERIMENT_ONE,
            command_type=CommandType.PAUSE,
            idempotency_key="web-repository-gateway-command",
            actor="sole_operator",
            reason="prove the web repository uses its fixed database gateway",
            payload={},
            confirmation="CONFIRM PAUSE",
            requested_at=NOW + timedelta(microseconds=1),
        )
        async with gateway_uow.begin() as uow:
            first_gateway_write = await uow.commands.enqueue(gateway_command)
        gateway_retry = OperatorCommand.request(
            command_id=UUID("55555555-5555-4555-8555-555555555557"),
            experiment_id=EXPERIMENT_ONE,
            command_type=CommandType.PAUSE,
            idempotency_key=gateway_command.idempotency_key,
            actor=gateway_command.actor,
            reason=gateway_command.reason,
            payload=gateway_command.payload,
            confirmation="CONFIRM PAUSE",
            requested_at=gateway_command.requested_at,
        )
        async with gateway_uow.begin() as uow:
            repeated_gateway_write = await uow.commands.enqueue(gateway_retry)
        assert first_gateway_write.created is True
        assert repeated_gateway_write.created is False
        assert repeated_gateway_write.command == gateway_command

        gateway_conflict = OperatorCommand.request(
            command_id=UUID("55555555-5555-4555-8555-555555555558"),
            experiment_id=EXPERIMENT_ONE,
            command_type=CommandType.PAUSE,
            idempotency_key=gateway_command.idempotency_key,
            actor=gateway_command.actor,
            reason="a materially different request cannot reuse this key",
            payload=gateway_command.payload,
            confirmation="CONFIRM PAUSE",
            requested_at=gateway_command.requested_at,
        )
        with pytest.raises(OperatorCommandConflict, match="idempotency key"):
            async with gateway_uow.begin() as uow:
                await uow.commands.enqueue(gateway_conflict)

        async with web.begin() as connection:
            audit_sequence = await connection.scalar(
                text(
                    "SELECT public.maais_append_audit_event("
                    ":event_id, :actor_reference, NULL, 'auth.login.succeeded', "
                    "'valid_credentials', '{\"authentication\":\"password\"}'::jsonb, "
                    "NULL, NULL, :occurred_at)"
                ),
                {
                    "event_id": WEB_AUDIT,
                    "actor_reference": pseudonymous_reference("actor", "sole_operator"),
                    "occurred_at": NOW,
                },
            )
            assert audit_sequence == 1
        await _expect_denied(
            web,
            "SELECT public.maais_append_audit_event("
            ":event_id, :actor_reference, NULL, 'backup.succeeded', NULL, '{}'::jsonb, "
            "NULL, NULL, :occurred_at)",
            {
                "event_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4"),
                "actor_reference": pseudonymous_reference("actor", "sole_operator"),
                "occurred_at": NOW,
            },
        )
        async with worker.begin() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT public.maais_append_audit_event("
                        ":event_id, :actor_reference, NULL, 'service.booted', "
                        "'runtime_registered', '{}'::jsonb, NULL, NULL, :occurred_at)"
                    ),
                    {
                        "event_id": WORKER_AUDIT,
                        "actor_reference": pseudonymous_reference("service", "worker-boot"),
                        "occurred_at": NOW + timedelta(microseconds=1),
                    },
                )
                == 2
            )
        async with operations.begin() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT public.maais_append_audit_event("
                        ":event_id, :actor_reference, NULL, 'readiness.verdict', "
                        "'qualification_pending', '{}'::jsonb, NULL, NULL, :occurred_at)"
                    ),
                    {
                        "event_id": OPERATIONS_AUDIT,
                        "actor_reference": pseudonymous_reference("service", "operations-boot"),
                        "occurred_at": NOW + timedelta(microseconds=2),
                    },
                )
                == 3
            )
        async with uow_factory.begin() as uow:
            audit = await uow.observability.verify_audit_chain()
            assert audit.ok is True
            assert audit.event_count == 3

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
            assert await connection.scalar(
                text(
                    "SELECT has_table_privilege(current_user, 'public.artifact_records', 'INSERT')"
                )
            )
            assert await connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, 'public.health_evaluations', 'INSERT')"
                )
            )
            assert not await connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, 'public.health_evaluations', 'UPDATE')"
                )
            )
            assert not await connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.audit_events', 'INSERT')")
            )
        await _expect_denied(worker, "INSERT INTO public.artifact_records DEFAULT VALUES")
        await _expect_denied(
            worker,
            "UPDATE public.audit_events SET reason_code = 'tampered' WHERE sequence = 1",
        )
        await _expect_denied(worker, "ALTER TABLE public.experiments ADD COLUMN forbidden int")
        async with verifier.connect() as connection:
            assert await connection.scalar(text("SHOW default_transaction_read_only")) == "on"
            assert await connection.scalar(text("SELECT count(*) FROM public.experiments")) == 1
            await connection.rollback()
        await _expect_denied(
            verifier,
            "INSERT INTO public.health_evaluations (evaluation_id) "
            "VALUES ('ffffffff-ffff-4fff-8fff-ffffffffffff')",
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
                expected_revision="0022",
                repository_root=Path(__file__).resolve().parents[2],
            )
            == "0022"
        )
    finally:
        for engine in engines:
            await engine.dispose()
        await _cleanup_roles(db_engine)


def _role_url(database_url: str, role_name: str, password: str):
    return make_url(database_url).set(username=role_name, password=password)


def _role_engine(database_url: str, role_name: str, password: str) -> AsyncEngine:
    return create_async_engine(_role_url(database_url, role_name, password), pool_pre_ping=True)


async def _replace_test_database(engine: AsyncEngine, database_name: str) -> None:
    await _drop_test_database(engine, database_name)
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))


async def _drop_test_database(engine: AsyncEngine, database_name: str) -> None:
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))


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
