from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from pydantic import SecretStr
from starlette.routing import Mount, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from maais.api.app import create_app
from maais.api.headers import PUBLIC_PRODUCTION_PATHS
from maais.api.security import SESSION_COOKIE_NAME
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.db.unit_of_work import UnitOfWork
from maais.security.passwords import hash_operator_password

pytestmark = pytest.mark.integration

PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret
ORIGIN = "https://mission-control.test"
NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
IDENTIFIER = "11111111-1111-4111-8111-111111111111"

PUBLIC_REGISTERED_HTTP = {
    ("GET", "/healthz/live"),
    ("GET", "/healthz/ready"),
    ("GET", "/monitor/v1/health"),
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/session"),
}
PROTECTED_HTTP = {
    ("POST", "/api/v1/auth/csrf"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/platform/candidates/{candidate_hash}"),
    ("GET", "/api/v1/runs/{run_id}"),
    ("GET", "/api/v1/runs/{run_id}/services"),
    ("GET", "/api/v1/runs/{run_id}/health"),
    ("GET", "/api/v1/runs/{run_id}/incidents"),
    ("GET", "/api/v1/runs/{run_id}/artifacts"),
    ("GET", "/api/v1/runs/{run_id}/audit"),
    ("GET", "/api/v1/experiments"),
    ("GET", "/api/v1/experiments/{experiment_id}/cloud-run"),
    ("GET", "/api/v1/experiments/{experiment_id}/overview"),
    ("GET", "/api/v1/experiments/{experiment_id}/decisions"),
    ("GET", "/api/v1/experiments/{experiment_id}/decisions/export.csv"),
    ("GET", "/api/v1/experiments/{experiment_id}/trades"),
    ("GET", "/api/v1/experiments/{experiment_id}/trades/export.csv"),
    ("GET", "/api/v1/decisions/{decision_id}"),
    ("GET", "/api/v1/decisions/{decision_id}/export.json"),
    ("GET", "/api/v1/experiments/{experiment_id}/research"),
    ("POST", "/api/v1/experiments/{experiment_id}/commands"),
    ("GET", "/api/v1/experiments/{experiment_id}/commands"),
    ("GET", "/api/v1/commands/{command_id}"),
    ("GET", "/api/v1/events"),
}


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _security_settings() -> SecuritySettings:
    return SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(PASSPHRASE)),
        session_pepper=SecretStr(
            "session-pepper-surface-0123456789-ABCDEFGHIJK"  # pragma: allowlist secret
        ),
        csrf_pepper=SecretStr(
            "csrf-pepper-surface-0123456789-ABCDEFGHIJKLMN"  # pragma: allowlist secret
        ),
        monitor_token=SecretStr(
            "monitor-token-surface-0123456789-ABCDEFGHIJK"  # pragma: allowlist secret
        ),
        secure_cookies=True,
        public_origin=ORIGIN,
    )


def _app(
    uow_factory: UnitOfWork,
    clock: MutableClock,
    *,
    dashboard_dir: Path | None = None,
):
    return create_app(
        uow_factory._session_factory,
        security_settings=_security_settings(),
        clock=clock,
        dashboard_dir=dashboard_dir,
    )


def _concrete_path(path: str) -> str:
    return (
        path.replace("{experiment_id}", IDENTIFIER)
        .replace("{candidate_hash}", "a" * 64)
        .replace("{run_id}", IDENTIFIER)
        .replace("{decision_id}", IDENTIFIER)
        .replace("{command_id}", IDENTIFIER)
    )


def test_every_production_route_is_explicitly_public_protected_or_static(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    (dashboard_dir / "index.html").write_text("MAAIS", encoding="utf-8")
    application = _app(
        uow_factory,
        MutableClock(NOW),
        dashboard_dir=dashboard_dir,
    )
    registered_http = {
        (method, route.path)
        for route in application.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method in {"GET", "POST"}
    }
    websocket_paths = {
        route.path
        for route in application.routes
        if isinstance(route, (APIWebSocketRoute, WebSocketRoute))
    }
    mounts = {route.path for route in application.routes if isinstance(route, Mount)}

    assert registered_http == PUBLIC_REGISTERED_HTTP | PROTECTED_HTTP
    assert {path for _, path in PUBLIC_REGISTERED_HTTP} <= PUBLIC_PRODUCTION_PATHS
    assert websocket_paths == {"/api/v1/events/stream"}
    assert mounts == {""}
    assert all(
        route.path not in {"/docs", "/openapi.json", "/redoc"} for route in application.routes
    )


async def test_every_protected_http_surface_rejects_unauthenticated_requests(
    uow_factory: UnitOfWork,
) -> None:
    application = _app(uow_factory, MutableClock(NOW))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    ) as client:
        responses = {
            (method, path): await client.request(method, _concrete_path(path))
            for method, path in sorted(PROTECTED_HTTP)
        }

    for route, response in responses.items():
        assert response.status_code == 401, route
        assert response.json() == {"detail": "session_authentication_required"}
        assert response.headers["cache-control"] == "no-store"


async def test_authenticated_get_and_export_surfaces_reach_domain_handlers(
    uow_factory: UnitOfWork,
) -> None:
    application = _app(uow_factory, MutableClock(NOW))
    get_routes = sorted(route for route in PROTECTED_HTTP if route[0] == "GET")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
        base_url=ORIGIN,
    ) as client:
        login = await client.post("/api/v1/auth/login", json={"password": PASSPHRASE})
        responses = {path: await client.get(_concrete_path(path)) for _, path in get_routes}

    assert login.status_code == 200
    for path, response in responses.items():
        assert response.status_code not in {401, 403}, path
        assert response.status_code < 500, path
        assert response.headers["cache-control"] == "no-store"


async def _open_websocket(
    application,
    *,
    cookie: str | None = None,
    disconnect_during_close: bool = False,
    await_first_message: bool = True,
):
    incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    headers: list[tuple[bytes, bytes]] = [(b"host", b"mission-control.test")]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode("ascii")))
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "scheme": "wss",
        "server": ("mission-control.test", 443),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
        "path": "/api/v1/events/stream",
        "raw_path": b"/api/v1/events/stream",
        "query_string": b"",
        "headers": headers,
        "subprotocols": [],
        "state": {},
        "extensions": {},
    }

    async def receive() -> dict[str, object]:
        return await incoming.get()

    async def send(message: dict[str, object]) -> None:
        if disconnect_during_close and message["type"] == "websocket.close":
            raise WebSocketDisconnect(code=1006)
        await outgoing.put(message)

    task = asyncio.create_task(application(scope, receive, send))
    if not await_first_message:
        await task
        return None, outgoing, task
    first_message = await asyncio.wait_for(outgoing.get(), timeout=2)
    return first_message, outgoing, task


async def test_websocket_rejects_before_accept_then_accepts_cookie_and_closes_on_expiry(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    application = _app(uow_factory, clock)
    unauthenticated, _, unauthenticated_task = await _open_websocket(application)
    await unauthenticated_task
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    ) as client:
        login = await client.post("/api/v1/auth/login", json={"password": PASSPHRASE})
        cookie = client.cookies.get(SESSION_COOKIE_NAME)
    accepted, outgoing, authenticated_task = await _open_websocket(
        application,
        cookie=f"{SESSION_COOKIE_NAME}={cookie}",
    )
    clock.value = NOW + timedelta(minutes=30)
    expired = await asyncio.wait_for(outgoing.get(), timeout=2)
    await authenticated_task

    assert login.status_code == 200
    assert unauthenticated == {
        "type": "websocket.close",
        "code": 1008,
        "reason": "session authentication required",
    }
    assert accepted["type"] == "websocket.accept"
    assert expired == {"type": "websocket.close", "code": 1008, "reason": "session expired"}


async def test_websocket_expiry_tolerates_peer_disconnect_during_close(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    application = _app(uow_factory, clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    ) as client:
        login = await client.post("/api/v1/auth/login", json={"password": PASSPHRASE})
        cookie = client.cookies.get(SESSION_COOKIE_NAME)

    accepted, _, websocket_task = await _open_websocket(
        application,
        cookie=f"{SESSION_COOKIE_NAME}={cookie}",
        disconnect_during_close=True,
    )
    clock.value = NOW + timedelta(minutes=30)

    await asyncio.wait_for(websocket_task, timeout=2)

    assert login.status_code == 200
    assert accepted["type"] == "websocket.accept"


async def test_websocket_rejection_tolerates_peer_disconnect_during_close(
    uow_factory: UnitOfWork,
) -> None:
    first_message, _, websocket_task = await _open_websocket(
        _app(uow_factory, MutableClock(NOW)),
        disconnect_during_close=True,
        await_first_message=False,
    )

    assert first_message is None
    assert websocket_task.done()
