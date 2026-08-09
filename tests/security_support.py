from __future__ import annotations

from functools import lru_cache

from maais.config.cloud import ServiceRole
from maais.config.security import AuthMode
from maais.security.passwords import hash_operator_password

TEST_OPERATOR_PASSPHRASE = "paper-only test operator passphrase"  # pragma: allowlist secret
TEST_SESSION_PEPPER = "session-pepper-test-only-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret
TEST_CSRF_PEPPER = "csrf-pepper-test-only-0123456789-ABCDEFGHIJKLM"  # pragma: allowlist secret
TEST_MONITOR_TOKEN = "monitor-token-test-only-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret


@lru_cache(maxsize=1)
def operator_password_hash_for_tests() -> str:
    return hash_operator_password(TEST_OPERATOR_PASSPHRASE)


def railway_security_values() -> dict[str, object]:
    return {
        "auth_mode": AuthMode.OPERATOR_SESSION,
        "operator_password_hash": operator_password_hash_for_tests(),
        "session_pepper": TEST_SESSION_PEPPER,
        "csrf_pepper": TEST_CSRF_PEPPER,
        "monitor_token": TEST_MONITOR_TOKEN,
        "operator_secure_cookies": True,
        "operator_public_origin": "https://mission-control.test",
    }


def railway_observability_values(service_role: ServiceRole) -> dict[str, object]:
    values: dict[str, object] = {
        "log_format": "json",
        "sentry_backend_dsn": (
            "https://backend-public-key@o0.ingest.sentry.io/123"  # pragma: allowlist secret
        ),
        "sentry_browser_dsn": (
            "https://browser-public-key@o0.ingest.sentry.io/456"  # pragma: allowlist secret
        ),
    }
    if service_role is ServiceRole.OPERATIONS:
        values.update(
            {
                "sentry_daily_close_monitor_slug": "maais-qualification-daily-close",
                "sentry_backup_monitor_slug": "maais-qualification-backup",
                "sentry_evidence_monitor_slug": "maais-qualification-evidence",
            }
        )
    return values
