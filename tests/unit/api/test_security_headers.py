from __future__ import annotations

from pathlib import Path

import httpx
from pydantic import SecretStr

from maais.api.app import create_app
from maais.api.headers import (
    CONTENT_SECURITY_POLICY,
    PERMISSIONS_POLICY,
    requires_operator_session,
)
from maais.config.cloud import DeploymentTarget
from maais.config.security import AuthMode, SecuritySettings
from maais.security.passwords import hash_operator_password

PASSPHRASE = "paper-only operator passphrase"  # pragma: allowlist secret


def test_unknown_api_monitor_and_health_paths_fail_closed() -> None:
    assert requires_operator_session("/api/v1/unclassified")
    assert requires_operator_session("/monitor/v1/unclassified")
    assert requires_operator_session("/healthz/unclassified")
    assert not requires_operator_session("/monitor/v1/health")
    assert not requires_operator_session("/assets/app-abcdef12.js")


def _production_security() -> SecuritySettings:
    return SecuritySettings(
        deployment_target=DeploymentTarget.RAILWAY,
        auth_mode=AuthMode.OPERATOR_SESSION,
        operator_password_hash=SecretStr(hash_operator_password(PASSPHRASE)),
        session_pepper=SecretStr("s" * 43),
        csrf_pepper=SecretStr("c" * 43),
        monitor_token=SecretStr("m" * 43),
        secure_cookies=True,
        public_origin="https://mission-control.test",
    )


async def test_production_security_headers_cover_public_error_responses() -> None:
    application = create_app(security_settings=_production_security())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://mission-control.test",
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"password": "x" * 257},
        )

    assert response.status_code == 422
    assert response.headers["strict-transport-security"] == ("max-age=31536000; includeSubDomains")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == PERMISSIONS_POLICY
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


async def test_hashed_assets_are_immutable_while_html_is_never_cached(tmp_path: Path) -> None:
    dashboard = tmp_path / "dist"
    assets = dashboard / "assets"
    assets.mkdir(parents=True)
    (dashboard / "index.html").write_text("<html>login shell</html>", encoding="utf-8")
    (assets / "app-abcdef12.js").write_text("export {};", encoding="utf-8")
    application = create_app(
        dashboard_dir=dashboard,
        security_settings=_production_security(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://mission-control.test",
    ) as client:
        html = await client.get("/")
        asset = await client.get("/assets/app-abcdef12.js")

    assert html.status_code == asset.status_code == 200
    assert html.headers["cache-control"] == "no-store"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_local_mode_has_narrow_dev_cors_and_no_hsts() -> None:
    application = create_app(
        control_token="local-test-token-0123456789abcdef",
        security_settings=SecuritySettings(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        local = await client.get(
            "/api/v1/auth/session",
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        docs = await client.get("/docs")

    assert "strict-transport-security" not in local.headers
    assert local.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert docs.status_code == 200


async def test_production_disables_docs_openapi_and_cross_origin_cors() -> None:
    application = create_app(security_settings=_production_security())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://mission-control.test",
    ) as client:
        docs = await client.get("/docs")
        openapi = await client.get("/openapi.json")
        preflight = await client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://evil.test",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert docs.status_code == openapi.status_code == 404
    assert "access-control-allow-origin" not in preflight.headers
