from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import uvicorn
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from maais.api.app import create_app
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.db.models.operations import OperatorCommandModel
from maais.db.unit_of_work import UnitOfWork
from maais.security.passwords import hash_operator_password
from tests.integration.conftest import _clean_all_tables
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def test_database_url() -> str:
    value = os.environ.get("MAAIS_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("MAAIS_TEST_DATABASE_URL is required for browser security tests")
    if not (make_url(value).database or "").endswith("_test"):
        pytest.fail("browser security database name must end with _test")
    return value


@pytest_asyncio.fixture
async def uow_factory(test_database_url: str) -> AsyncIterator[UnitOfWork]:
    engine: AsyncEngine = create_async_engine(test_database_url, pool_pre_ping=True)
    await _clean_all_tables(engine)
    yield UnitOfWork(async_sessionmaker(engine, expire_on_commit=False))
    await _clean_all_tables(engine)
    await engine.dispose()


class AdvancingClock:
    def __init__(self, value: datetime) -> None:
        self._value = value
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._value

    def advance(self, delta: timedelta) -> None:
        with self._lock:
            self._value += delta


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _temporary_certificate(directory: Path) -> tuple[Path, Path]:
    certificate = directory / "certificate.pem"
    private_key = directory / "private-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    private_key.chmod(0o600)
    return certificate, private_key


def _wait_for_server(base_url: str, server: uvicorn.Server) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if server.started:
            try:
                response = httpx.get(base_url, verify=False, timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
        time.sleep(0.1)
    raise AssertionError("HTTPS Mission Control test server did not become ready")


async def test_real_browser_proves_private_session_csrf_expiry_and_logout(
    uow_factory: UnitOfWork,
    test_database_url: str,
    tmp_path: Path,
) -> None:
    dashboard = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
    playwright_cli = (
        Path(__file__).resolve().parents[2]
        / "dashboard"
        / "node_modules"
        / ".bin"
        / "playwright-cli"
    )
    if not (dashboard / "index.html").is_file():
        pytest.fail("dashboard production build is required for browser security tests")
    if not playwright_cli.is_file():
        pytest.fail("dashboard Playwright CLI dependency is required for browser security tests")

    async with uow_factory.begin() as uow:
        await uow.experiments.create(_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0021"))

    port = _available_port()
    base_url = f"https://127.0.0.1:{port}"
    passphrase = secrets.token_urlsafe(32)
    settings = SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(passphrase)),
        session_pepper=SecretStr(secrets.token_urlsafe(48)),
        csrf_pepper=SecretStr(secrets.token_urlsafe(48)),
        monitor_token=SecretStr(secrets.token_urlsafe(48)),
        secure_cookies=True,
        public_origin=base_url,
    )
    clock = AdvancingClock(datetime.now(timezone.utc))
    certificate, private_key = _temporary_certificate(tmp_path)
    server_holder: dict[str, uvicorn.Server] = {}

    def serve() -> None:
        engine = create_async_engine(test_database_url, pool_pre_ping=True)
        application = create_app(
            async_sessionmaker(engine, expire_on_commit=False),
            dashboard_dir=dashboard,
            security_settings=settings,
            clock=clock,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                ssl_certfile=str(certificate),
                ssl_keyfile=str(private_key),
                log_level="warning",
                access_log=False,
            )
        )
        server_holder["server"] = server
        try:
            server.run()
        finally:
            asyncio.run(engine.dispose())

    server_thread = threading.Thread(target=serve, name="maais-browser-test-server")
    server_thread.start()
    deadline = time.monotonic() + 10
    while "server" not in server_holder and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    server = server_holder.get("server")
    if server is None:
        pytest.fail("Mission Control test server failed before startup")

    config = tmp_path / "playwright-cli.json"
    config.write_text(
        json.dumps(
            {
                "browser": {
                    "browserName": "chromium",
                    "isolated": True,
                    "contextOptions": {"ignoreHTTPSErrors": True},
                }
            }
        ),
        encoding="utf-8",
    )
    expiry_marker = tmp_path / "advance-clock.request"
    expiry_continue = tmp_path / "advance-clock.done"
    coordinator_stop = threading.Event()

    def expire_session_when_requested() -> None:
        deadline_at = time.monotonic() + 45
        while time.monotonic() < deadline_at and not coordinator_stop.is_set():
            if expiry_marker.exists():
                clock.advance(timedelta(minutes=31))
                expiry_continue.touch()
                return
            time.sleep(0.05)

    coordinator = threading.Thread(
        target=expire_session_when_requested,
        name="maais-browser-test-clock",
    )
    coordinator.start()

    environment = os.environ.copy()
    environment.update(
        {
            "MAAIS_BROWSER_SMOKE_TEST_ONLY": "1",
            "MAAIS_BROWSER_SMOKE_BASE_URL": base_url,
            "MAAIS_BROWSER_SMOKE_EXPERIMENT_ID": str(EXPERIMENT_ID),
            "MAAIS_BROWSER_SMOKE_PASSPHRASE": passphrase,
            "MAAIS_BROWSER_SMOKE_CONFIG": str(config),
            "MAAIS_BROWSER_SMOKE_ARTIFACT_DIR": str(tmp_path / "browser"),
            "MAAIS_BROWSER_SMOKE_EXPIRY_MARKER": str(expiry_marker),
            "MAAIS_BROWSER_SMOKE_EXPIRY_CONTINUE": str(expiry_continue),
        }
    )
    smoke = Path(__file__).resolve().parents[2] / "scripts" / "browser-smoke.sh"
    try:
        await asyncio.to_thread(_wait_for_server, base_url, server)
        completed = await asyncio.to_thread(
            subprocess.run,
            [str(smoke)],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    finally:
        coordinator_stop.set()
        coordinator.join(timeout=2)
        server.should_exit = True
        server_thread.join(timeout=10)

    assert not coordinator.is_alive()
    assert not server_thread.is_alive()
    assert completed.returncode == 0, (
        f"browser smoke failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert passphrase not in completed.stdout
    assert passphrase not in completed.stderr
    assert "browser auth smoke passed" in completed.stdout
    verification_engine = create_async_engine(test_database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(verification_engine, expire_on_commit=False)() as session:
            commands = (await session.scalars(select(OperatorCommandModel))).all()
    finally:
        await verification_engine.dispose()
    assert len(commands) == 1
    assert commands[0].command_type == "pause"
    assert commands[0].actor == "sole_operator"
    assert commands[0].reason == "browser security smoke"
