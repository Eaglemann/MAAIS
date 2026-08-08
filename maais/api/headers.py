"""Frozen Mission Control route classification and browser response policy."""

from __future__ import annotations

import re

from fastapi import Request, Response

PUBLIC_PRODUCTION_PATHS = frozenset(
    {
        "/healthz/live",
        "/healthz/ready",
        "/monitor/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/session",
    }
)

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
    )
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
_HASHED_ASSET = re.compile(r"^/assets/.+-[0-9A-Za-z_-]{8,}\.[A-Za-z0-9]+$")


def requires_operator_session(path: str) -> bool:
    return path.startswith(("/api/v1/", "/monitor/", "/healthz/")) and (
        path not in PUBLIC_PRODUCTION_PATHS
    )


def apply_browser_headers(
    request: Request,
    response: Response,
    *,
    production: bool,
) -> Response:
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    path = request.url.path
    if path.startswith(("/api/", "/monitor/", "/healthz/")) or path in ("/", ""):
        response.headers["Cache-Control"] = "no-store"
    elif _HASHED_ASSET.fullmatch(path):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response
