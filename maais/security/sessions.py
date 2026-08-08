from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import SecretStr

from maais.config.security import AuthMode as _AuthMode

AuthMode = _AuthMode

SESSION_TOKEN_BYTES = 32
SESSION_ABSOLUTE_TTL = timedelta(hours=12)
SESSION_IDLE_TTL = timedelta(minutes=30)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT = timedelta(minutes=30)
INVALID_SESSION = "invalid_session"


class SessionAuthenticationError(RuntimeError):
    public_error_code = INVALID_SESSION

    def __init__(self) -> None:
        super().__init__("session authentication failed")


@dataclass(frozen=True, slots=True)
class OperatorSession:
    id: UUID
    token_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    actor: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        _require_hash(self.token_hash, "session token")
        _require_hash(self.csrf_hash, "CSRF token")
        if self.token_hash == self.csrf_hash:
            raise ValueError("session and CSRF token hashes must be independent")
        if (
            not self.actor.isascii()
            or self.actor != self.actor.strip()
            or not 1 <= len(self.actor) <= 64
        ):
            raise ValueError("session actor must be 1-64 trimmed ASCII characters")
        for name, value in (
            ("created_at", self.created_at),
            ("last_seen_at", self.last_seen_at),
            ("expires_at", self.expires_at),
        ):
            _require_utc(value, name)
        if self.revoked_at is not None:
            _require_utc(self.revoked_at, "revoked_at")
        if not self.created_at <= self.last_seen_at <= self.expires_at:
            raise ValueError("session timestamps are out of order")
        if self.expires_at != self.created_at + SESSION_ABSOLUTE_TTL:
            raise ValueError("session absolute TTL must remain fixed at 12 hours")
        if self.revoked_at is not None and self.revoked_at < self.last_seen_at:
            raise ValueError("session revocation cannot precede last activity")
        if self.version < 1:
            raise ValueError("session version must be positive")

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def revoke(self, revoked_at: datetime) -> OperatorSession:
        _require_utc(revoked_at, "revoked_at")
        if self.revoked_at is not None:
            return self
        if revoked_at < self.last_seen_at:
            raise ValueError("session revocation cannot precede last activity")
        return replace(
            self,
            revoked_at=revoked_at,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class NewSessionRequest:
    id: UUID
    token_hash: str = field(repr=False)
    csrf_hash: str = field(repr=False)
    actor: str
    created_at: datetime
    expires_at: datetime

    def to_session(self) -> OperatorSession:
        return OperatorSession(
            id=self.id,
            token_hash=self.token_hash,
            csrf_hash=self.csrf_hash,
            actor=self.actor,
            created_at=self.created_at,
            last_seen_at=self.created_at,
            expires_at=self.expires_at,
            revoked_at=None,
            version=1,
        )


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: OperatorSession
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def to_request(self) -> NewSessionRequest:
        return NewSessionRequest(
            id=self.session.id,
            token_hash=self.session.token_hash,
            csrf_hash=self.session.csrf_hash,
            actor=self.session.actor,
            created_at=self.session.created_at,
            expires_at=self.session.expires_at,
        )


@dataclass(frozen=True, slots=True)
class OperatorAuthState:
    failed_attempts: int
    window_started_at: datetime | None
    locked_until: datetime | None
    updated_at: datetime
    version: int

    def __post_init__(self) -> None:
        _require_utc(self.updated_at, "updated_at")
        if self.window_started_at is not None:
            _require_utc(self.window_started_at, "window_started_at")
        if self.locked_until is not None:
            _require_utc(self.locked_until, "locked_until")
        if not 0 <= self.failed_attempts <= LOGIN_MAX_FAILURES or self.version < 1:
            raise ValueError("login throttle counts and version must be valid")
        if self.failed_attempts == 0:
            if self.window_started_at is not None or self.locked_until is not None:
                raise ValueError("clean login throttle state cannot retain a window or lockout")
        elif self.window_started_at is None:
            raise ValueError("failed login state requires a window")
        elif self.failed_attempts < LOGIN_MAX_FAILURES and self.locked_until is not None:
            raise ValueError("login throttle cannot lock before the fifth failure")
        elif self.failed_attempts == LOGIN_MAX_FAILURES and (
            self.locked_until is None or self.locked_until != self.updated_at + LOGIN_LOCKOUT
        ):
            raise ValueError("the fifth login failure requires the fixed lockout")
        if self.window_started_at is not None and self.updated_at < self.window_started_at:
            raise ValueError("login throttle timestamps are out of order")

    def locked(self, observed_at: datetime) -> bool:
        _require_utc(observed_at, "observed_at")
        return self.locked_until is not None and observed_at < self.locked_until


class SessionRepository(Protocol):
    async def issue(self, request: NewSessionRequest) -> OperatorSession:
        raise NotImplementedError

    async def authenticate(
        self,
        token_hash: str,
        *,
        observed_at: datetime,
    ) -> OperatorSession:
        raise NotImplementedError

    async def revoke(
        self,
        session_id: UUID,
        *,
        revoked_at: datetime,
    ) -> OperatorSession:
        raise NotImplementedError


def opaque_token_hash(token: str, pepper: SecretStr) -> str:
    if not token or not token.isascii():
        raise ValueError("opaque token must be nonempty ASCII")
    return hmac.new(
        pepper.get_secret_value().encode("utf-8"),
        token.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def issue_session_tokens(
    *,
    actor: str,
    observed_at: datetime,
    session_pepper: SecretStr,
    csrf_pepper: SecretStr,
    session_id: UUID | None = None,
) -> IssuedSession:
    _require_utc(observed_at, "observed_at")
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    csrf_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    while hmac.compare_digest(token, csrf_token):  # pragma: no cover - cryptographic guard
        csrf_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    session = OperatorSession(
        id=session_id or uuid4(),
        token_hash=opaque_token_hash(token, session_pepper),
        csrf_hash=opaque_token_hash(csrf_token, csrf_pepper),
        actor=actor,
        created_at=observed_at,
        last_seen_at=observed_at,
        expires_at=observed_at + SESSION_ABSOLUTE_TTL,
        revoked_at=None,
        version=1,
    )
    return IssuedSession(session=session, token=token, csrf_token=csrf_token)


def rotate_session_tokens(
    session: OperatorSession,
    *,
    observed_at: datetime,
    session_pepper: SecretStr,
    csrf_pepper: SecretStr,
) -> IssuedSession:
    require_authenticatable_session(session, observed_at=observed_at)
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    csrf_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    while hmac.compare_digest(token, csrf_token):  # pragma: no cover - cryptographic guard
        csrf_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    rotated = replace(
        session,
        token_hash=opaque_token_hash(token, session_pepper),
        csrf_hash=opaque_token_hash(csrf_token, csrf_pepper),
        last_seen_at=observed_at,
        version=session.version + 1,
    )
    return IssuedSession(session=rotated, token=token, csrf_token=csrf_token)


def require_authenticatable_session(
    session: OperatorSession | None,
    *,
    observed_at: datetime,
) -> OperatorSession:
    _require_utc(observed_at, "observed_at")
    if (
        session is None
        or not session.active
        or observed_at >= session.expires_at
        or observed_at >= session.last_seen_at + SESSION_IDLE_TTL
    ):
        raise SessionAuthenticationError
    return session


def verify_csrf_token(
    presented_token: str,
    expected_hash: str,
    pepper: SecretStr,
) -> bool:
    try:
        presented_hash = opaque_token_hash(presented_token, pepper)
    except ValueError:
        presented_hash = opaque_token_hash("invalid", pepper)
    return hmac.compare_digest(presented_hash, expected_hash)


def _require_hash(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} hash must be a lowercase SHA-256 digest")


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.astimezone(timezone.utc) != value:
        raise ValueError(f"{name} must be normalized to UTC")
