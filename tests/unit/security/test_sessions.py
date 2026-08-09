from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr

from maais.security.sessions import (
    INVALID_SESSION,
    LOGIN_LOCKOUT,
    LOGIN_MAX_FAILURES,
    SESSION_ABSOLUTE_TTL,
    SESSION_IDLE_TTL,
    OperatorAuthState,
    SessionAuthenticationError,
    issue_session_tokens,
    opaque_token_hash,
    require_authenticatable_session,
    rotate_session_tokens,
    verify_csrf_token,
)

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
SESSION_PEPPER = SecretStr(
    "session-pepper-unit-test-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret
)
CSRF_PEPPER = SecretStr(
    "csrf-pepper-unit-test-0123456789-ABCDEFGHIJKLM"  # pragma: allowlist secret
)


def _decoded_size(token: str) -> int:
    return len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_issued_tokens_are_independent_high_entropy_and_only_hashes_enter_session() -> None:
    issued = issue_session_tokens(
        actor="sole_operator",
        observed_at=NOW,
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )

    assert issued.token != issued.csrf_token
    assert _decoded_size(issued.token) >= 32
    assert _decoded_size(issued.csrf_token) >= 32
    assert issued.session.token_hash == opaque_token_hash(issued.token, SESSION_PEPPER)
    assert issued.session.csrf_hash == opaque_token_hash(issued.csrf_token, CSRF_PEPPER)
    assert issued.token not in repr(issued.session)
    assert issued.csrf_token not in repr(issued.session)
    assert issued.session.created_at == NOW
    assert issued.session.last_seen_at == NOW
    assert issued.session.expires_at == NOW + SESSION_ABSOLUTE_TTL
    assert issued.session.version == 1


def test_session_timestamps_must_be_utc_and_actor_is_bounded() -> None:
    issued = issue_session_tokens(
        actor="sole_operator",
        observed_at=NOW,
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )
    with pytest.raises(ValueError, match="UTC"):
        issue_session_tokens(
            actor="sole_operator",
            observed_at=NOW.replace(tzinfo=None),
            session_pepper=SESSION_PEPPER,
            csrf_pepper=CSRF_PEPPER,
        )
    with pytest.raises(ValueError, match="actor"):
        issue_session_tokens(
            actor=" ",
            observed_at=NOW,
            session_pepper=SESSION_PEPPER,
            csrf_pepper=CSRF_PEPPER,
        )
    with pytest.raises(ValueError, match="12 hours"):
        replace(issued.session, expires_at=issued.session.expires_at + timedelta(seconds=1))


@pytest.mark.parametrize(
    "session_mutation",
    (
        lambda session: replace(
            session,
            last_seen_at=NOW - SESSION_IDLE_TTL - timedelta(microseconds=1),
        ),
        lambda session: replace(
            session,
            created_at=NOW - SESSION_ABSOLUTE_TTL - timedelta(microseconds=1),
            expires_at=NOW - timedelta(microseconds=1),
            last_seen_at=NOW - timedelta(seconds=1),
        ),
        lambda session: session.revoke(NOW),
    ),
)
def test_idle_absolute_and_revoked_sessions_share_one_public_error(
    session_mutation,
) -> None:
    issued = issue_session_tokens(
        actor="sole_operator",
        observed_at=NOW - timedelta(hours=1),
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )
    invalid = session_mutation(issued.session)

    with pytest.raises(SessionAuthenticationError) as error:
        require_authenticatable_session(invalid, observed_at=NOW)

    assert error.value.public_error_code == INVALID_SESSION
    assert str(error.value) == "session authentication failed"


def test_rotation_changes_both_tokens_preserves_absolute_expiry_and_increments_version() -> None:
    issued = issue_session_tokens(
        actor="sole_operator",
        observed_at=NOW,
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )
    rotated = rotate_session_tokens(
        issued.session,
        observed_at=NOW + timedelta(minutes=5),
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )

    assert rotated.token != issued.token
    assert rotated.csrf_token != issued.csrf_token
    assert rotated.session.id == issued.session.id
    assert rotated.session.created_at == issued.session.created_at
    assert rotated.session.expires_at == issued.session.expires_at
    assert rotated.session.last_seen_at == NOW + timedelta(minutes=5)
    assert rotated.session.version == issued.session.version + 1
    assert verify_csrf_token(rotated.csrf_token, rotated.session.csrf_hash, CSRF_PEPPER)
    assert not verify_csrf_token(issued.csrf_token, rotated.session.csrf_hash, CSRF_PEPPER)
    assert not verify_csrf_token("malformed-\N{LOCK}", rotated.session.csrf_hash, CSRF_PEPPER)


def test_revocation_is_idempotent_and_does_not_replace_original_evidence() -> None:
    session = issue_session_tokens(
        actor="sole_operator",
        observed_at=NOW,
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    ).session
    first = session.revoke(NOW + timedelta(minutes=1))
    repeated = first.revoke(NOW + timedelta(minutes=2))

    assert repeated == first
    assert repeated.revoked_at == NOW + timedelta(minutes=1)
    assert repeated.version == session.version + 1


def test_login_throttle_domain_requires_lockout_exactly_on_fifth_failure() -> None:
    with pytest.raises(ValueError, match="fifth"):
        OperatorAuthState(
            failed_attempts=LOGIN_MAX_FAILURES,
            window_started_at=NOW,
            locked_until=None,
            updated_at=NOW,
            version=1,
        )
    locked = OperatorAuthState(
        failed_attempts=LOGIN_MAX_FAILURES,
        window_started_at=NOW,
        locked_until=NOW + LOGIN_LOCKOUT,
        updated_at=NOW,
        version=1,
    )

    assert locked.locked(NOW)
    assert not locked.locked(NOW + LOGIN_LOCKOUT)
