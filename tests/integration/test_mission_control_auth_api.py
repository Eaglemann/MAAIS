from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from maais.api.app import create_app
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.db.models.auth import OperatorSessionModel
from maais.db.models.operations import OperatorCommandModel
from maais.db.unit_of_work import UnitOfWork
from maais.security.passwords import hash_operator_password
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret
WRONG_PASSWORD = "wrong paper-only operator passphrase"  # pragma: allowlist secret
ORIGIN = "https://mission-control.test"
HOST = "mission-control.test"
NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")


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
            "session-pepper-auth-api-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret
        ),
        csrf_pepper=SecretStr(
            "csrf-pepper-auth-api-0123456789-ABCDEFGHIJKLM"  # pragma: allowlist secret
        ),
        monitor_token=SecretStr(
            "monitor-token-auth-api-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret
        ),
        secure_cookies=True,
        public_origin=ORIGIN,
    )


def _app(uow_factory: UnitOfWork, clock: MutableClock):
    return create_app(
        uow_factory._session_factory,
        security_settings=_security_settings(),
        clock=clock,
    )


def _origin_headers(*, csrf_token: str | None = None) -> dict[str, str]:
    headers = {"Origin": ORIGIN, "Host": HOST}
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    return headers


async def _login(client: httpx.AsyncClient, password: str = PASSPHRASE) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"password": password})


async def _prepare_experiment(uow_factory: UnitOfWork) -> None:
    async with uow_factory.begin() as uow:
        await uow.experiments.create(_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0022"))


async def _audit_events(uow_factory: UnitOfWork):
    async with uow_factory.begin() as uow:
        return await uow.observability.list_audit_events()


async def test_cloud_login_sets_secure_cookie_and_exposes_secret_free_session_view(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(uow_factory, clock)),
        base_url=ORIGIN,
    ) as client:
        login = await _login(client)
        session = await client.get("/api/v1/auth/session")

    cookie = login.headers["set-cookie"]
    assert login.status_code == 200
    assert login.json()["actor"] == "sole_operator"
    assert login.json()["csrf_token"]
    assert "password" not in login.text.lower()
    assert "__Host-maais_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Domain=" not in cookie
    assert session.status_code == 200
    assert session.json() == {
        "authenticated": True,
        "actor": "sole_operator",
        "auth_mode": "operator_session",
        "expires_at": (NOW + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
    }
    assert session.headers["cache-control"] == "no-store"
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == ["auth.login.succeeded"]
    assert audit[0].actor_reference.startswith("actor:")
    assert audit[0].session_reference is not None
    assert PASSPHRASE not in json.dumps(audit[0].to_json_data(), sort_keys=True)


async def test_invalid_password_and_lockout_have_one_public_response_and_persist_count(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(uow_factory, clock)),
        base_url=ORIGIN,
    ) as client:
        failures = [await _login(client, WRONG_PASSWORD) for _ in range(5)]
        locked_correct = await _login(client, PASSPHRASE)

    expected = {"detail": "invalid_credentials"}
    assert all(response.status_code == 401 for response in (*failures, locked_correct))
    assert all(response.json() == expected for response in (*failures, locked_correct))
    assert all(PASSPHRASE not in response.text for response in (*failures, locked_correct))
    async with uow_factory.begin() as uow:
        state = await uow.sessions.login_state(observed_at=NOW)
    assert state.failed_attempts == 5
    assert state.locked_until == NOW + timedelta(minutes=30)
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == [
        "auth.login.rejected",
        "auth.login.rejected",
        "auth.login.rejected",
        "auth.login.rejected",
        "auth.login.locked",
        "auth.login.locked",
    ]


async def test_login_payload_errors_never_echo_password() -> None:
    settings = _security_settings()
    application = create_app(security_settings=settings, clock=MutableClock(NOW))
    raw_password = "do-not-echo-" + "x" * 300  # pragma: allowlist secret
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url=ORIGIN,
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"password": raw_password, "unexpected": True},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_login_payload"}
    assert raw_password not in response.text


async def test_csrf_bootstrap_requires_session_and_exact_origin_then_rotates_only_csrf(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(uow_factory, clock)),
        base_url=ORIGIN,
    ) as client:
        unauthenticated = await client.post(
            "/api/v1/auth/csrf",
            headers=_origin_headers(),
        )
        login = await _login(client)
        first_csrf = login.json()["csrf_token"]
        session_cookie = client.cookies.get("__Host-maais_session")
        missing_origin = await client.post("/api/v1/auth/csrf")
        wrong_origin = await client.post(
            "/api/v1/auth/csrf",
            headers={"Origin": "https://evil.test", "Host": HOST},
        )
        rotated = await client.post(
            "/api/v1/auth/csrf",
            headers=_origin_headers(),
        )

    assert unauthenticated.status_code == 401
    assert missing_origin.status_code == 403
    assert wrong_origin.status_code == 403
    assert rotated.status_code == 200
    assert rotated.json()["csrf_token"] != first_csrf
    assert client.cookies.get("__Host-maais_session") == session_cookie
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == [
        "auth.login.succeeded",
        "auth.csrf.rejected",
        "auth.csrf.rejected",
    ]


async def test_logout_requires_current_csrf_and_revokes_session(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(uow_factory, clock)),
        base_url=ORIGIN,
    ) as client:
        login = await _login(client)
        old_csrf = login.json()["csrf_token"]
        rotated = await client.post("/api/v1/auth/csrf", headers=_origin_headers())
        current_csrf = rotated.json()["csrf_token"]
        missing = await client.post("/api/v1/auth/logout", headers=_origin_headers())
        mismatch = await client.post(
            "/api/v1/auth/logout",
            headers=_origin_headers(csrf_token=old_csrf),
        )
        logout = await client.post(
            "/api/v1/auth/logout",
            headers=_origin_headers(csrf_token=current_csrf),
        )
        session = await client.get("/api/v1/auth/session")

    assert missing.status_code == mismatch.status_code == 403
    assert missing.json() == mismatch.json() == {"detail": "csrf_verification_failed"}
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert session.json()["authenticated"] is False
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == [
        "auth.login.succeeded",
        "auth.csrf.rejected",
        "auth.csrf.rejected",
        "auth.session.revoked",
        "auth.logout",
    ]


async def test_cloud_command_requires_session_origin_and_matching_csrf(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_experiment(uow_factory)
    clock = MutableClock(NOW)
    body = {
        "command_type": "emergency_halt",
        "idempotency_key": "cloud-auth-command-0001",
        "reason": "operator observed abnormal behavior",
        "payload": {"source": "mission_control"},
        "confirmation": "CONFIRM EMERGENCY_HALT",
    }
    path = f"/api/v1/experiments/{EXPERIMENT_ID}/commands"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(uow_factory, clock)),
        base_url=ORIGIN,
    ) as client:
        login = await _login(client)
        csrf_token = login.json()["csrf_token"]
        missing = await client.post(path, json=body, headers=_origin_headers())
        mismatch = await client.post(
            path,
            json=body,
            headers=_origin_headers(csrf_token="wrong-csrf-token"),
        )
        created = await client.post(
            path,
            json=body,
            headers=_origin_headers(csrf_token=csrf_token),
        )
        repeated = await client.post(
            path,
            json=body,
            headers=_origin_headers(csrf_token=csrf_token),
        )

    assert missing.status_code == mismatch.status_code == 403
    assert created.status_code == repeated.status_code == 202
    assert created.json()["actor"] == "sole_operator"
    assert repeated.json() == created.json()
    async with uow_factory.begin() as uow:
        command_count = await uow.session.scalar(
            select(func.count()).select_from(OperatorCommandModel)
        )
    assert command_count == 1
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == [
        "auth.login.succeeded",
        "auth.csrf.rejected",
        "auth.csrf.rejected",
        "operator.command.enqueued",
    ]
    assert audit[-1].evidence["command_id"] == created.json()["command_id"]


async def test_idle_expiry_and_cloud_bearer_rejection_are_fail_closed(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(uow_factory, clock)),
        base_url=ORIGIN,
    ) as client:
        login = await _login(client)
        bearer = await client.post(
            "/api/v1/auth/csrf",
            headers={**_origin_headers(), "Authorization": "Bearer forbidden-in-cloud"},
        )
        clock.value = NOW + timedelta(minutes=30)
        expired = await client.get("/api/v1/auth/session")

    assert login.status_code == 200
    assert bearer.status_code == 401
    assert bearer.json() == {"detail": "session_authentication_required"}
    assert expired.status_code == 200
    assert expired.json()["authenticated"] is False
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == [
        "auth.login.succeeded",
        "auth.session.expired",
    ]


async def test_successful_login_revokes_every_prior_active_operator_session(
    uow_factory: UnitOfWork,
) -> None:
    clock = MutableClock(NOW)
    application = _app(uow_factory, clock)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=ORIGIN,
        ) as first,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=ORIGIN,
        ) as second,
    ):
        first_login = await _login(first)
        clock.value = NOW + timedelta(seconds=1)
        second_login = await _login(second)
        first_session = await first.get("/api/v1/auth/session")
        second_session = await second.get("/api/v1/auth/session")

    assert first_login.status_code == second_login.status_code == 200
    assert first_session.json()["authenticated"] is False
    assert second_session.json()["authenticated"] is True
    async with uow_factory.begin() as uow:
        active_count = await uow.session.scalar(
            select(func.count())
            .select_from(OperatorSessionModel)
            .where(OperatorSessionModel.revoked_at.is_(None))
        )
    assert active_count == 1
    audit = await _audit_events(uow_factory)
    assert [event.event_code for event in audit] == [
        "auth.login.succeeded",
        "auth.session.revoked",
        "auth.login.succeeded",
    ]
