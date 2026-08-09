from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from maais.api.app import create_app
from maais.artifacts.models import (
    GENESIS_EVIDENCE_HASH,
    ArtifactPublicationAttempt,
    ArtifactRecord,
    ArtifactType,
    RetentionRequest,
    ScheduledOperation,
    ScheduledOperationType,
    StoredArtifact,
)
from maais.config.artifacts import RetentionMode
from maais.config.cloud import DeploymentTarget, ServiceRole
from maais.config.security import AuthMode, SecuritySettings
from maais.db.models.observability import HealthEvaluationModel
from maais.db.unit_of_work import UnitOfWork
from maais.observability.audit import HealthEvaluation, HealthSeverity, HealthStatus
from maais.operations.cloud_health import CloudHealthEvaluator
from maais.security.passwords import hash_operator_password
from tests.integration.test_health_supervisor import SnapshotReader
from tests.integration.test_platform_repository import (
    COMMAND_ONE,
    EXPERIMENT_ONE,
    NOW,
    RUN_ONE,
    WORKER_ONE,
    _descriptor,
    _prepare_activatable_run,
    _service,
)

pytestmark = pytest.mark.integration

PASSPHRASE = "cloud evidence passphrase"  # pragma: allowlist secret
ORIGIN = "https://mission-control.test"
OPERATIONS_BOOT = UUID("99999999-9999-4999-8999-999999999999")
WEB_BOOT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ATTEMPT_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ARTIFACT_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def _security_settings() -> SecuritySettings:
    return SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(PASSPHRASE)),
        session_pepper=SecretStr(
            "cloud-session-pepper-0123456789-ABCDEFGHIJKLM"  # pragma: allowlist secret
        ),
        csrf_pepper=SecretStr(
            "cloud-csrf-pepper-0123456789-ABCDEFGHIJKLMNO"  # pragma: allowlist secret
        ),
        monitor_token=SecretStr(
            "cloud-monitor-token-0123456789-ABCDEFGHIJKLMN"  # pragma: allowlist secret
        ),
        secure_cookies=True,
        public_origin=ORIGIN,
    )


def _client(uow_factory: UnitOfWork) -> httpx.AsyncClient:
    application = create_app(
        uow_factory._session_factory,
        security_settings=_security_settings(),
        clock=lambda: NOW + timedelta(hours=1),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    )


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/auth/login", json={"password": PASSPHRASE})
    assert response.status_code == 200


async def _prepare_cloud_evidence(uow_factory: UnitOfWork) -> tuple[str, str]:
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
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ONE,
                boot_id=OPERATIONS_BOOT,
                role=ServiceRole.OPERATIONS,
                service_id="operations-service",
            )
        )
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ONE,
                boot_id=WEB_BOOT,
                role=ServiceRole.WEB,
                service_id="mission-control-service",
            )
        )

    health_reader = SnapshotReader()
    health_reader.failed = ("worker_lease",)
    evaluator = CloudHealthEvaluator(
        uow_factory=uow_factory,
        snapshot_reader=health_reader,
        service_boot_id=OPERATIONS_BOOT,
    )
    await evaluator.evaluate(RUN_ONE, NOW + timedelta(minutes=1))
    health_reader.failed = ()
    await evaluator.evaluate(RUN_ONE, NOW + timedelta(minutes=2))

    operation = ScheduledOperation.start(
        operation_id=OPERATION_ID,
        run_id=RUN_ONE,
        experiment_id=EXPERIMENT_ONE,
        operation_type=ScheduledOperationType.DAILY_REPORT,
        berlin_date=date(2026, 8, 8),
        owner_boot_id=OPERATIONS_BOOT,
        generated_at=NOW + timedelta(minutes=3),
        started_at=NOW + timedelta(minutes=3, seconds=1),
    )
    attempt = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_ID,
        operation_id=OPERATION_ID,
        attempt=1,
        bundle_content_hash="1" * 64,
        started_at=NOW + timedelta(minutes=3, seconds=2),
    )
    key = (
        "maais/production/"
        f"{_descriptor().descriptor_hash}/{EXPERIMENT_ONE}/daily_report/report-001/report.json"
    )
    replica = _stored_artifact(
        store_name="railway-replica",
        key=key,
        version_id=None,
    )
    canonical = _stored_artifact(
        store_name="canonical-worm",
        key=key,
        version_id="canonical-version-001",
    )
    record = ArtifactRecord.create(
        record_id=ARTIFACT_ID,
        operation_id=OPERATION_ID,
        publication_attempt_id=ATTEMPT_ID,
        environment="production",
        candidate_hash=_descriptor().descriptor_hash,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        artifact_type=ArtifactType.DAILY_REPORT,
        report_id="report-001",
        bundle_content_hash="1" * 64,
        size_bytes=128,
        media_type="application/json",
        generated_at=NOW + timedelta(minutes=3),
        recorded_at=NOW + timedelta(minutes=4),
        producing_deployment_id="deployment-1",
        producing_service_id="operations-service",
        sequence=1,
        replica_inventory=(replica,),
        canonical_inventory=(canonical,),
        previous_evidence_hash=GENESIS_EVIDENCE_HASH,
    )
    async with uow_factory.begin() as uow:
        await uow.scheduled_operations.acquire(operation)
        await uow.artifacts.start_attempt(attempt)
        await uow.artifacts.record_publication(record)
        await uow.scheduled_operations.complete(
            OPERATION_ID,
            owner_boot_id=OPERATIONS_BOOT,
            result_artifact_ids=(ARTIFACT_ID,),
            completed_at=NOW + timedelta(minutes=5),
        )
    return _descriptor().descriptor_hash, record.catalog_content_hash


def _stored_artifact(
    *,
    store_name: str,
    key: str,
    version_id: str | None,
) -> StoredArtifact:
    return StoredArtifact(
        store_name=store_name,
        key=key,
        etag='"verified-etag"',
        version_id=version_id,
        sha256="1" * 64,
        size_bytes=128,
        content_type="application/json",
        retention=RetentionRequest(
            mode=RetentionMode.COMPLIANCE,
            retain_until=datetime(2026, 11, 8, tzinfo=timezone.utc),
        ),
        stored_at=NOW + timedelta(minutes=3, seconds=30),
    )


def _assert_no_forbidden_metadata(value: object) -> None:
    forbidden = {
        "authorization",
        "cookie",
        "csrf",
        "database_url",
        "dsn",
        "ip_address",
        "password_hash",
        "private_key",
        "provider_secret",
        "raw_exception",
        "token",
        "user_agent",
    }
    if isinstance(value, Mapping):
        assert forbidden.isdisjoint(str(key).casefold() for key in value)
        for nested in value.values():
            _assert_no_forbidden_metadata(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_forbidden_metadata(nested)


async def test_authenticated_cloud_evidence_is_exact_scoped_paginated_and_secret_free(
    uow_factory: UnitOfWork,
) -> None:
    candidate_hash, artifact_hash = await _prepare_cloud_evidence(uow_factory)
    async with _client(uow_factory) as client:
        unauthorized = await client.get(f"/api/v1/platform/candidates/{candidate_hash}")
        await _login(client)
        candidate = await client.get(f"/api/v1/platform/candidates/{candidate_hash}")
        run = await client.get(f"/api/v1/runs/{RUN_ONE}")
        discovered = await client.get(f"/api/v1/experiments/{EXPERIMENT_ONE}/cloud-run")
        services = await client.get(f"/api/v1/runs/{RUN_ONE}/services?limit=1")
        next_services = await client.get(
            f"/api/v1/runs/{RUN_ONE}/services",
            params={
                "limit": 1,
                "before_at": services.json()["next_before_at"],
                "before_id": services.json()["next_before_id"],
            },
        )
        health = await client.get(f"/api/v1/runs/{RUN_ONE}/health?limit=1")
        next_health = await client.get(
            f"/api/v1/runs/{RUN_ONE}/health",
            params={
                "limit": 1,
                "before_at": health.json()["next_before_at"],
                "before_id": health.json()["next_before_id"],
            },
        )
        incidents = await client.get(f"/api/v1/runs/{RUN_ONE}/incidents?limit=1")
        artifacts = await client.get(f"/api/v1/runs/{RUN_ONE}/artifacts?limit=1")
        audit = await client.get(f"/api/v1/runs/{RUN_ONE}/audit?limit=1")
        next_audit = await client.get(
            f"/api/v1/runs/{RUN_ONE}/audit",
            params={"limit": 1, "before_sequence": audit.json()["next_before_sequence"]},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.json() == {"detail": "session_authentication_required"}
    responses = (
        candidate,
        run,
        discovered,
        services,
        next_services,
        health,
        next_health,
        incidents,
        artifacts,
        audit,
        next_audit,
    )
    failures = [
        (response.request.url.path, response.status_code, response.text)
        for response in responses
        if response.status_code != 200
    ]
    assert not failures, failures
    assert all(response.headers["cache-control"] == "no-store" for response in responses)

    candidate_body = candidate.json()
    assert candidate_body["descriptor_hash"] == candidate_hash
    assert candidate_body["git_sha"] == "a" * 40
    assert candidate_body["schema_revision"] == "0022"
    assert candidate_body["creator_deployment_id"] == "deployment-1"
    assert candidate_body["qualification_evidence_hash"] == "f" * 64

    assert run.json() == discovered.json()
    assert run.json()["id"] == str(RUN_ONE)
    assert run.json()["experiment_id"] == str(EXPERIMENT_ONE)
    assert run.json()["database_system_identifier"] == "7669409277984608290"
    assert run.json()["railway_environment_id"] == "environment-1"
    assert run.json()["status"] == "active"
    assert len(run.json()["incidents"]) == 1

    first_service = services.json()["items"][0]
    second_service = next_services.json()["items"][0]
    assert services.json()["has_more"] is True
    assert first_service["boot_id"] != second_service["boot_id"]
    assert first_service["deployment_id"] == "deployment-1"
    assert first_service["replica_id"].startswith("replica-")

    assert health.json()["items"][0]["overall_status"] == "healthy"
    assert health.json()["items"][0]["content_hash"]
    assert (
        health.json()["items"][0]["evaluation_id"]
        != (next_health.json()["items"][0]["evaluation_id"])
    )
    assert incidents.json()["items"][0]["status"] == "resolved"
    assert incidents.json()["has_more"] is False
    artifact = artifacts.json()["items"][0]
    assert artifact["catalog_content_hash"] == artifact_hash
    assert artifact["canonical_inventory"][0]["version_id"] == "canonical-version-001"
    assert artifact["canonical_inventory"][0]["retention_mode"] == "COMPLIANCE"
    assert artifact["canonical_inventory"][0]["retain_until"].startswith("2026-11-08")
    assert artifacts.json()["has_more"] is False
    assert artifacts.json()["next_before_sequence"] is None
    assert audit.json()["items"][0]["run_id"] == str(RUN_ONE)
    assert audit.json()["items"][0]["sequence"] != next_audit.json()["items"][0]["sequence"]
    for response in responses:
        _assert_no_forbidden_metadata(response.json())


async def test_experiment_cloud_run_discovery_is_nullable_without_browser_visible_404(
    uow_factory: UnitOfWork,
) -> None:
    async with _client(uow_factory) as client:
        await _login(client)
        response = await client.get(f"/api/v1/experiments/{EXPERIMENT_ONE}/cloud-run")

    assert response.status_code == 200
    assert response.json() is None
    assert response.headers["cache-control"] == "no-store"


async def test_cloud_evidence_rejects_forbidden_metadata_without_leaking_payload(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_cloud_evidence(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.observability.record_health(
            HealthEvaluation.create(
                evaluation_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                run_id=RUN_ONE,
                service_boot_id=OPERATIONS_BOOT,
                overall_status=HealthStatus.HEALTHY,
                failed_check_names=(),
                severity=HealthSeverity.INFO,
                deduplication_key="2" * 64,
                incident_id=None,
                recovery_of_evaluation_id=None,
                recovered_at=None,
                components={
                    "raw_exception": "private-provider-credential-canary",
                },
                checked_at=NOW + timedelta(minutes=6),
            )
        )

    async with _client(uow_factory) as client:
        await _login(client)
        response = await client.get(f"/api/v1/runs/{RUN_ONE}/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "cloud_evidence_integrity_failed"}
    assert "private-provider-credential-canary" not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_cloud_evidence_hash_failure_is_generic_and_never_returns_tampered_state(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_cloud_evidence(uow_factory)
    async with uow_factory.begin() as uow:
        uow.session.add(
            HealthEvaluationModel(
                evaluation_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                run_id=RUN_ONE,
                service_boot_id=OPERATIONS_BOOT,
                overall_status="healthy",
                failed_check_names=[],
                severity="info",
                deduplication_key="2" * 64,
                incident_id=None,
                recovery_of_evaluation_id=None,
                recovered_at=None,
                component_json={"database": {"passed": True}},
                checked_at=NOW + timedelta(minutes=6),
                content_hash="3" * 64,
            )
        )

    async with _client(uow_factory) as client:
        await _login(client)
        response = await client.get(f"/api/v1/runs/{RUN_ONE}/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "cloud_evidence_integrity_failed"}
    assert "database" not in response.text
    assert response.headers["cache-control"] == "no-store"
