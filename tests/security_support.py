from __future__ import annotations

from functools import lru_cache

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
    }
