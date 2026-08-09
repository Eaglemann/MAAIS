from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

from maais.api.app import create_app
from maais.api.health import MONITOR_COMPONENTS, DatabaseCloudEndpointReader
from maais.config.cloud import DeploymentTarget, ServiceRole
from maais.config.security import AuthMode, SecuritySettings
from maais.db.unit_of_work import UnitOfWork
from maais.operations.cloud_health import CloudHealthEvaluator
from maais.security.passwords import hash_operator_password
from tests.integration.test_health_supervisor import OPERATIONS_BOOT, SnapshotReader
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

ORIGIN = "https://mission-control.test"
PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret
MONITOR_TOKEN = "monitor-token-integration-0123456789-ABCDEFG"  # pragma: allowlist secret


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _security() -> SecuritySettings:
    return SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(PASSPHRASE)),
        session_pepper=SecretStr(
            "session-pepper-monitor-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret
        ),
        csrf_pepper=SecretStr(
            "csrf-pepper-monitor-0123456789-ABCDEFGHIJKLM"  # pragma: allowlist secret
        ),
        monitor_token=SecretStr(MONITOR_TOKEN),
        secure_cookies=True,
        public_origin=ORIGIN,
    )


async def test_monitor_projects_latest_immutable_health_and_readiness_without_trade_data(
    uow_factory: UnitOfWork,
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
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ONE,
                boot_id=OPERATIONS_BOOT,
                role=ServiceRole.OPERATIONS,
                service_id="operations-service",
            )
        )

    health_reader = SnapshotReader()
    evaluator = CloudHealthEvaluator(
        uow_factory=uow_factory,
        snapshot_reader=health_reader,
        service_boot_id=OPERATIONS_BOOT,
    )
    healthy_at = NOW + timedelta(minutes=1)
    await evaluator.evaluate(RUN_ONE, healthy_at)

    clock = MutableClock(healthy_at + timedelta(seconds=10))
    endpoint_reader = DatabaseCloudEndpointReader(
        session_factory=uow_factory._session_factory,
        expected_schema_revision="0022",
        expected_candidate_hash=_descriptor().descriptor_hash,
        railway_environment_id="environment-1",
        boot_verified=True,
        clock=clock,
    )
    application = create_app(
        uow_factory._session_factory,
        security_settings=_security(),
        cloud_health_reader=endpoint_reader,
        clock=clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    ) as client:
        ready = await client.get("/healthz/ready")
        healthy = await client.get(
            "/monitor/v1/health",
            headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
        )

        health_reader.failed = ("worker_lease", "worm_replication")
        failed_at = healthy_at + timedelta(minutes=1)
        await evaluator.evaluate(RUN_ONE, failed_at)
        clock.value = failed_at + timedelta(seconds=10)
        degraded = await client.get(
            "/monitor/v1/health",
            headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
        )

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert healthy.status_code == 200
    assert healthy.json() == {
        "status": "ok",
        **{name: True for name in MONITOR_COMPONENTS},
    }
    assert degraded.status_code == 503
    assert degraded.json() == {
        "status": "degraded",
        "database": True,
        "worker": False,
        "ledger": True,
        "cursors": True,
        "operations": True,
        "evidence_replication": False,
        "daily_close": True,
    }
    forbidden = (
        str(EXPERIMENT_ONE),
        str(RUN_ONE),
        _descriptor().descriptor_hash,
        "BTCUSDT",
        "worker_lease",
        "worm_replication",
    )
    assert all(value not in degraded.text for value in forbidden)


async def test_readiness_fails_closed_for_boot_candidate_or_schema_mismatch(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_activatable_run(
        uow_factory,
        experiment_id=EXPERIMENT_ONE,
        run_id=RUN_ONE,
        command_id=COMMAND_ONE,
        worker_boot_id=WORKER_ONE,
    )
    cases = (
        {"boot_verified": False},
        {"expected_candidate_hash": "0" * 64},
        {"expected_schema_revision": "9999"},
    )
    for changes in cases:
        values: dict[str, object] = {
            "session_factory": uow_factory._session_factory,
            "expected_schema_revision": "0022",
            "expected_candidate_hash": _descriptor().descriptor_hash,
            "railway_environment_id": "environment-1",
            "boot_verified": True,
            "clock": lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
        }
        values.update(changes)
        reader = DatabaseCloudEndpointReader(**values)
        application = create_app(
            uow_factory._session_factory,
            security_settings=_security(),
            cloud_health_reader=reader,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=ORIGIN,
        ) as client:
            response = await client.get("/healthz/ready")

        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
        assert response.headers["cache-control"] == "no-store"
