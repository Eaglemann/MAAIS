from __future__ import annotations

from collections.abc import Mapping

import httpx
from pydantic import SecretStr

from maais.api.app import create_app
from maais.api.health import (
    MONITOR_COMPONENTS,
    CloudEndpointSnapshot,
    InMemoryMonitorRateLimiter,
)
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.security.passwords import hash_operator_password

ORIGIN = "https://mission-control.test"
PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret
MONITOR_TOKEN = "monitor-token-endpoint-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret


def _security() -> SecuritySettings:
    return SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(PASSPHRASE)),
        session_pepper=SecretStr(
            "session-pepper-endpoint-0123456789-ABCDEFGHI"  # pragma: allowlist secret
        ),
        csrf_pepper=SecretStr(
            "csrf-pepper-endpoint-0123456789-ABCDEFGHIJK"  # pragma: allowlist secret
        ),
        monitor_token=SecretStr(MONITOR_TOKEN),
        secure_cookies=True,
        public_origin=ORIGIN,
    )


def _snapshot(**changes: bool) -> CloudEndpointSnapshot:
    components: dict[str, bool] = {name: True for name in MONITOR_COMPONENTS}
    components.update(changes)
    return CloudEndpointSnapshot(ready=True, components=components)


class StubHealthReader:
    def __init__(
        self,
        snapshot: CloudEndpointSnapshot | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot or _snapshot()
        self.error = error
        self.readiness_calls = 0
        self.monitor_calls = 0

    async def readiness(self) -> bool:
        self.readiness_calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot.ready

    async def monitor(self) -> CloudEndpointSnapshot:
        self.monitor_calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


async def _client(reader: StubHealthReader, **application_options: object):
    application = create_app(
        security_settings=_security(),
        cloud_health_reader=reader,
        **application_options,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    )


async def test_liveness_is_process_only_and_all_public_health_bodies_are_exact() -> None:
    reader = StubHealthReader()
    async with await _client(reader) as client:
        live = await client.get("/healthz/live")
        ready = await client.get("/healthz/ready")
        monitor = await client.get(
            "/monitor/v1/health",
            headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
        )

    assert live.status_code == ready.status_code == monitor.status_code == 200
    assert live.json() == {"status": "live"}
    assert ready.json() == {"status": "ready"}
    assert monitor.json() == {
        "status": "ok",
        "database": True,
        "worker": True,
        "ledger": True,
        "cursors": True,
        "operations": True,
        "evidence_replication": True,
        "daily_close": True,
    }
    assert set(monitor.json()) == MONITOR_COMPONENTS | {"status"}
    assert reader.readiness_calls == reader.monitor_calls == 1
    assert all(
        response.headers["cache-control"] == "no-store" for response in (live, ready, monitor)
    )


async def test_unconfigured_cloud_authority_is_live_but_never_ready_or_healthy() -> None:
    application = create_app(security_settings=_security())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    ) as client:
        live = await client.get("/healthz/live")
        ready = await client.get("/healthz/ready")
        monitor = await client.get(
            "/monitor/v1/health",
            headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
        )

    assert live.status_code == 200
    assert ready.status_code == monitor.status_code == 503
    assert ready.json() == {"status": "not_ready"}
    assert monitor.json() == {
        "status": "degraded",
        **{name: False for name in MONITOR_COMPONENTS},
    }


async def test_failed_or_unavailable_dependencies_return_only_generic_503_bodies() -> None:
    canary = "private-database-run-identifier"
    unavailable = StubHealthReader(error=RuntimeError(canary))
    async with await _client(unavailable) as client:
        live = await client.get("/healthz/live")
        ready = await client.get("/healthz/ready")
        failed_read = await client.get(
            "/monitor/v1/health",
            headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
        )

    assert live.status_code == 200
    assert live.json() == {"status": "live"}
    assert ready.status_code == failed_read.status_code == 503
    assert ready.json() == {"status": "not_ready"}
    assert failed_read.json() == {
        "status": "degraded",
        **{name: False for name in MONITOR_COMPONENTS},
    }
    assert canary not in ready.text
    assert canary not in failed_read.text

    degraded = StubHealthReader(_snapshot(worker=False))
    async with await _client(degraded) as client:
        response = await client.get(
            "/monitor/v1/health",
            headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
        )
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["worker"] is False


async def test_monitor_secret_is_independent_constant_surface_and_checked_before_read() -> None:
    reader = StubHealthReader()
    async with await _client(reader) as client:
        missing = await client.get("/monitor/v1/health")
        wrong = await client.get(
            "/monitor/v1/health",
            headers={
                "Cookie": "maais_session=operator-session-is-not-monitor-authority",
                "X-MAAIS-Monitor-Token": "wrong-monitor-token",
            },
        )
        duplicated = await client.get(
            "/monitor/v1/health",
            headers=[
                ("X-MAAIS-Monitor-Token", MONITOR_TOKEN),
                ("X-MAAIS-Monitor-Token", MONITOR_TOKEN),
            ],
        )

    for response in (missing, wrong, duplicated):
        assert response.status_code == 404
        assert response.json() == {"detail": "not_found"}
        assert response.headers["cache-control"] == "no-store"
        assert MONITOR_TOKEN not in response.text
    assert reader.monitor_calls == 0


async def test_monitor_has_an_independent_bounded_in_memory_rate_limit() -> None:
    elapsed = 10.0
    limiter = InMemoryMonitorRateLimiter(
        maximum_requests=2,
        window_seconds=60,
        monotonic=lambda: elapsed,
    )
    reader = StubHealthReader()
    async with await _client(reader, monitor_rate_limiter=limiter) as client:
        responses = [
            await client.get(
                "/monitor/v1/health",
                headers={"X-MAAIS-Monitor-Token": MONITOR_TOKEN},
            )
            for _ in range(3)
        ]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].json() == {"detail": "rate_limited"}
    assert responses[-1].headers["cache-control"] == "no-store"
    assert reader.monitor_calls == 2


def test_endpoint_snapshot_rejects_missing_extra_or_non_boolean_components() -> None:
    healthy: Mapping[str, bool] = {name: True for name in MONITOR_COMPONENTS}
    for components in (
        {name: value for name, value in healthy.items() if name != "ledger"},
        {**healthy, "secret_detail": True},
        {**healthy, "database": 1},
    ):
        try:
            CloudEndpointSnapshot(ready=True, components=components)
        except (TypeError, ValueError):
            pass
        else:  # pragma: no cover - makes the contract failure explicit
            raise AssertionError("invalid public monitor component contract was accepted")
