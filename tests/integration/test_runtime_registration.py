from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import maais.platform.services as platform_services
from maais.config.cloud import EU_WEST_RAILWAY_REGION, DeploymentTarget, ServiceRole
from maais.config.settings import Settings
from maais.db.models.platform import PlatformCandidateModel, ServiceInstanceModel
from maais.operations.migrations import bootstrap_roles_with_url
from maais.platform.runtime import (
    RuntimeIdentityError,
    heartbeat_registered_runtime,
    stop_registered_runtime,
    verify_and_register_runtime_evidence,
)
from tests.integration.database_role_support import (
    cleanup_database_roles,
    integration_role_passwords,
)
from tests.security_support import railway_observability_values, railway_security_values
from tests.unit.platform.test_registry_domain import _descriptor

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
ROLE_BOOT_IDS = {
    ServiceRole.WEB: UUID("11111111-1111-4111-8111-111111111111"),
    ServiceRole.WORKER: UUID("22222222-2222-4222-8222-222222222222"),
    ServiceRole.OPERATIONS: UUID("33333333-3333-4333-8333-333333333333"),
    ServiceRole.VERIFIER: UUID("44444444-4444-4444-8444-444444444444"),
    ServiceRole.MIGRATOR: UUID("55555555-5555-4555-8555-555555555555"),
}


def _settings(
    candidate_path: Path,
    *,
    service_role: ServiceRole = ServiceRole.WORKER,
    service_id: str = "worker-service",
    database_url: str | None = None,
) -> Settings:
    database_roles = {
        ServiceRole.WORKER: "maais_worker",
        ServiceRole.MIGRATOR: "maais_migrator",
        ServiceRole.WEB: "maais_web",
        ServiceRole.OPERATIONS: "maais_ops",
        ServiceRole.VERIFIER: "maais_verifier",
    }
    values: dict[str, object] = {
        **railway_observability_values(service_role),
        "deployment_target": DeploymentTarget.RAILWAY,
        "run_mode": "paper_live",
        "environment": "qualification",
        "service_role": service_role,
        "railway_project_id": "project-1",
        "railway_environment_id": "environment-1",
        "railway_service_id": service_id,
        "railway_deployment_id": "deployment-1",
        "railway_snapshot_id": "snapshot-1",
        "railway_replica_id": f"replica-{service_role.value}",
        "railway_region": EU_WEST_RAILWAY_REGION,
        "expected_railway_region": EU_WEST_RAILWAY_REGION,
        "railway_git_commit_sha": _descriptor().git_sha,
        "candidate_descriptor_path": candidate_path,
        "expected_schema_revision": "0022",
        "database_role_name": database_roles[service_role],
        "_env_file": None,
    }
    if database_url is not None:
        values["database_url"] = database_url
    if service_role is ServiceRole.WEB:
        values.update(railway_security_values())
    if service_role in {ServiceRole.WORKER, ServiceRole.OPERATIONS, ServiceRole.VERIFIER}:
        values["cloud_run_id"] = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    if service_role is ServiceRole.WORKER:
        values.update(
            {
                "manifest_artifact_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                "artifact_store_mode": "canonical_read",
                "artifact_canonical_endpoint_url": "https://s3.worm-provider.example",
                "artifact_canonical_region": "eu-central-1",
                "artifact_canonical_bucket": "maais-canonical",
                "artifact_canonical_access_key": (
                    "canonical-read-access"  # pragma: allowlist secret
                ),
                "artifact_canonical_secret_key": (
                    "canonical-read-secret"  # pragma: allowlist secret
                ),
            }
        )
    if service_role is ServiceRole.OPERATIONS:
        values.update(
            {
                "artifact_store_mode": "dual_s3",
                "artifact_replica_endpoint_url": "https://storage.railway.example",
                "artifact_replica_region": "auto",
                "artifact_replica_bucket": "maais-replica",
                "artifact_replica_access_key": "replica-access",  # pragma: allowlist secret
                "artifact_replica_secret_key": "replica-secret",  # pragma: allowlist secret
                "artifact_canonical_endpoint_url": "https://s3.worm-provider.example",
                "artifact_canonical_region": "eu-central-1",
                "artifact_canonical_bucket": "maais-canonical",
                "artifact_canonical_access_key": (
                    "canonical-write-access"  # pragma: allowlist secret
                ),
                "artifact_canonical_secret_key": (
                    "canonical-write-secret"  # pragma: allowlist secret
                ),
            }
        )
    return Settings(**values)


async def test_migrator_catalogs_exact_candidate_once_across_deployment_retries(
    db_engine,
    test_database_url: str,
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(descriptor.to_json_data()), encoding="utf-8")
    passwords = integration_role_passwords()
    role_engine = None
    try:
        await bootstrap_roles_with_url(test_database_url, passwords)
        migrator_url = (
            make_url(test_database_url).set(
                username="maais_migrator",
                password=passwords.migrator,
            )
        ).render_as_string(hide_password=False)
        first_settings = _settings(
            candidate_path,
            service_role=ServiceRole.MIGRATOR,
            service_id="migrator-service",
            database_url=migrator_url,
        )
        second_settings = first_settings.model_copy(
            update={"railway_deployment_id": "deployment-2"}
        )

        first = await platform_services.ensure_cloud_migrator_candidate(
            first_settings,
            clock=lambda: NOW,
        )
        second = await platform_services.ensure_cloud_migrator_candidate(
            second_settings,
            clock=lambda: NOW + timedelta(seconds=1),
        )

        async with db_engine.connect() as connection:
            rows = tuple(
                (
                    await connection.execute(
                        select(
                            PlatformCandidateModel.descriptor_hash,
                            PlatformCandidateModel.descriptor_json,
                            PlatformCandidateModel.creator_deployment_id,
                            PlatformCandidateModel.registered_at,
                        )
                    )
                ).all()
            )
            await connection.rollback()
        assert first == descriptor
        assert second == descriptor
        assert len(rows) == 1
        assert rows[0].descriptor_hash == descriptor.descriptor_hash
        assert rows[0].descriptor_json == descriptor.to_json_data()
        assert rows[0].creator_deployment_id == "deployment-1"
        assert rows[0].registered_at == NOW

        role_engine = create_async_engine(migrator_url, pool_pre_ping=True)
        evidence = await verify_and_register_runtime_evidence(
            settings=second_settings,
            session_factory=async_sessionmaker(role_engine, expire_on_commit=False),
            descriptor=descriptor,
            boot_id=ROLE_BOOT_IDS[ServiceRole.MIGRATOR],
            started_at=NOW + timedelta(seconds=2),
            run_id=None,
        )
        assert evidence.identity.candidate_hash == descriptor.descriptor_hash
    finally:
        if role_engine is not None:
            await role_engine.dispose()
        await cleanup_database_roles(db_engine)


async def test_runtime_registration_uses_real_database_identity_and_freezes_boot(
    db_engine,
    uow_factory,
    test_database_url: str,
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(descriptor.to_json_data()), encoding="utf-8")
    async with uow_factory.begin() as uow:
        await uow.platform.register_candidate(
            descriptor,
            creator_deployment_id="deployment-1",
            registered_at=NOW,
        )

    passwords = integration_role_passwords()
    engines = []
    try:
        await bootstrap_roles_with_url(test_database_url, passwords)
        role_connections = {
            ServiceRole.WEB: ("maais_web", passwords.web),
            ServiceRole.WORKER: ("maais_worker", passwords.worker),
            ServiceRole.OPERATIONS: ("maais_ops", passwords.operations),
            ServiceRole.VERIFIER: ("maais_verifier", passwords.verifier),
            ServiceRole.MIGRATOR: ("maais_migrator", passwords.migrator),
        }
        role_engines = {
            role: create_async_engine(
                make_url(test_database_url).set(username=username, password=password),
                pool_pre_ping=True,
            )
            for role, (username, password) in role_connections.items()
        }
        engines.extend(role_engines.values())

        registered = {}
        for role, role_engine in role_engines.items():
            evidence = await verify_and_register_runtime_evidence(
                settings=_settings(
                    candidate_path,
                    service_role=role,
                    service_id=f"{role.value}-service",
                ),
                session_factory=async_sessionmaker(role_engine, expire_on_commit=False),
                descriptor=descriptor,
                boot_id=ROLE_BOOT_IDS[role],
                started_at=NOW,
                run_id=None,
            )
            assert evidence.identity.boot_id == ROLE_BOOT_IDS[role]
            assert evidence.identity.service_role is role
            assert evidence.schema_revision == "0022"
            assert len(evidence.database_system_identifier_sha256) == 64
            registered[role] = evidence.identity

        for role, role_engine in role_engines.items():
            await heartbeat_registered_runtime(
                session_factory=async_sessionmaker(role_engine, expire_on_commit=False),
                identity=registered[role],
                sequence=1,
                heartbeat_at=NOW + timedelta(seconds=1),
            )
            await stop_registered_runtime(
                session_factory=async_sessionmaker(role_engine, expire_on_commit=False),
                identity=registered[role],
                reason_code="clean_shutdown",
                stopped_at=NOW + timedelta(seconds=2),
            )
        await stop_registered_runtime(
            session_factory=async_sessionmaker(
                role_engines[ServiceRole.WORKER],
                expire_on_commit=False,
            ),
            identity=registered[ServiceRole.WORKER],
            reason_code="clean_shutdown",
            stopped_at=NOW + timedelta(seconds=2),
        )

        async with db_engine.connect() as connection:
            rows = tuple(
                (
                    await connection.execute(
                        select(
                            ServiceInstanceModel.boot_id,
                            ServiceInstanceModel.heartbeat_sequence,
                            ServiceInstanceModel.last_heartbeat_at,
                        ).order_by(ServiceInstanceModel.boot_id)
                    )
                ).all()
            )
            await connection.rollback()
        assert {row.boot_id for row in rows} == set(ROLE_BOOT_IDS.values())
        assert all(row.heartbeat_sequence == 1 for row in rows)
        assert all(row.last_heartbeat_at == NOW + timedelta(seconds=1) for row in rows)
        async with uow_factory.begin() as uow:
            audit = await uow.observability.list_audit_events()
        assert [event.event_code for event in audit] == [
            *(["service.booted"] * 5),
            *(["service.stopped"] * 5),
        ]
        assert [event.source_role.value for event in audit] == [
            "web",
            "worker",
            "operations",
            "verifier",
            "migrator",
            "web",
            "worker",
            "operations",
            "verifier",
            "migrator",
        ]
        assert {event.service_boot_id for event in audit} == set(ROLE_BOOT_IDS.values())

        with pytest.raises(RuntimeIdentityError, match="database role"):
            await verify_and_register_runtime_evidence(
                settings=_settings(
                    candidate_path,
                    service_role=ServiceRole.WEB,
                    service_id="web-service",
                ),
                session_factory=async_sessionmaker(
                    role_engines[ServiceRole.WORKER],
                    expire_on_commit=False,
                ),
                descriptor=descriptor,
                boot_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                started_at=NOW,
                run_id=None,
            )

        with pytest.raises(RuntimeIdentityError, match="boot identity conflicts"):
            await verify_and_register_runtime_evidence(
                settings=_settings(candidate_path, service_id="replacement-worker"),
                session_factory=async_sessionmaker(
                    role_engines[ServiceRole.WORKER],
                    expire_on_commit=False,
                ),
                descriptor=descriptor,
                boot_id=ROLE_BOOT_IDS[ServiceRole.WORKER],
                started_at=NOW,
                run_id=None,
            )
    finally:
        for engine in engines:
            await engine.dispose()
        await cleanup_database_roles(db_engine)
