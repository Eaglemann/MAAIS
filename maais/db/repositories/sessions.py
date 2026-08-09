"""Row-locked persistence for opaque operator sessions and login throttling."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.auth import OperatorAuthStateModel, OperatorSessionModel
from maais.security.passwords import INVALID_CREDENTIALS
from maais.security.sessions import (
    LOGIN_LOCKOUT,
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW,
    NewSessionRequest,
    OperatorAuthState,
    OperatorSession,
    SessionAuthenticationError,
    require_authenticatable_session,
)


class SessionConflict(RuntimeError):
    pass


class LoginAuthenticationError(RuntimeError):
    public_error_code = INVALID_CREDENTIALS

    def __init__(self) -> None:
        super().__init__("operator authentication failed")


class OperatorSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(self, request: NewSessionRequest) -> OperatorSession:
        candidate = request.to_session()
        created = await self._session.scalar(
            insert(OperatorSessionModel)
            .values(**_session_values(candidate))
            .on_conflict_do_nothing()
            .returning(OperatorSessionModel.id)
        )
        if created is not None:
            return candidate
        row = await self._session.scalar(
            select(OperatorSessionModel)
            .where(
                or_(
                    OperatorSessionModel.id == candidate.id,
                    OperatorSessionModel.token_hash == candidate.token_hash,
                    OperatorSessionModel.csrf_hash == candidate.csrf_hash,
                )
            )
            .with_for_update()
        )
        if row is not None and _session_from_row(row) == candidate:
            return candidate
        raise SessionConflict("operator session identifier or token hash conflicts")

    async def authenticate(
        self,
        token_hash: str,
        *,
        observed_at: datetime,
    ) -> OperatorSession:
        row = await self._session.scalar(
            select(OperatorSessionModel)
            .where(OperatorSessionModel.token_hash == token_hash)
            .with_for_update()
        )
        current = _session_from_row(row) if row is not None else None
        authenticated = require_authenticatable_session(current, observed_at=observed_at)
        updated = replace(
            authenticated,
            last_seen_at=observed_at,
            version=authenticated.version + 1,
        )
        assert row is not None
        row.last_seen_at = updated.last_seen_at
        row.version = updated.version
        return updated

    async def check(self, token_hash: str, *, observed_at: datetime) -> OperatorSession:
        row = await self._session.scalar(
            select(OperatorSessionModel).where(OperatorSessionModel.token_hash == token_hash)
        )
        current = _session_from_row(row) if row is not None else None
        return require_authenticatable_session(current, observed_at=observed_at)

    async def rotate(self, candidate: OperatorSession) -> OperatorSession:
        row = await self._locked_session(candidate.id)
        current = _session_from_row(row)
        require_authenticatable_session(current, observed_at=candidate.last_seen_at)
        if (
            candidate.actor != current.actor
            or candidate.created_at != current.created_at
            or candidate.expires_at != current.expires_at
            or candidate.revoked_at is not None
            or candidate.version != current.version + 1
            or candidate.last_seen_at < current.last_seen_at
            or candidate.token_hash == current.token_hash
            or candidate.csrf_hash == current.csrf_hash
        ):
            raise SessionConflict("session rotation evidence is stale or invalid")
        hash_owner = await self._session.scalar(
            select(OperatorSessionModel.id)
            .where(
                OperatorSessionModel.id != candidate.id,
                or_(
                    OperatorSessionModel.token_hash == candidate.token_hash,
                    OperatorSessionModel.csrf_hash == candidate.csrf_hash,
                ),
            )
            .with_for_update()
        )
        if hash_owner is not None:
            raise SessionConflict("session rotation token hash conflicts")
        row.token_hash = candidate.token_hash
        row.csrf_hash = candidate.csrf_hash
        row.last_seen_at = candidate.last_seen_at
        row.version = candidate.version
        return candidate

    async def revoke(
        self,
        session_id: UUID,
        *,
        revoked_at: datetime,
    ) -> OperatorSession:
        row = await self._locked_session(session_id)
        current = _session_from_row(row)
        updated = current.revoke(revoked_at)
        row.revoked_at = updated.revoked_at
        row.version = updated.version
        return updated

    async def rotate_csrf(self, candidate: OperatorSession) -> OperatorSession:
        row = await self._locked_session(candidate.id)
        current = _session_from_row(row)
        require_authenticatable_session(current, observed_at=candidate.last_seen_at)
        if (
            candidate.actor != current.actor
            or candidate.created_at != current.created_at
            or candidate.expires_at != current.expires_at
            or candidate.revoked_at is not None
            or candidate.version != current.version + 1
            or candidate.last_seen_at < current.last_seen_at
            or candidate.token_hash != current.token_hash
            or candidate.csrf_hash == current.csrf_hash
        ):
            raise SessionConflict("CSRF rotation evidence is stale or invalid")
        hash_owner = await self._session.scalar(
            select(OperatorSessionModel.id)
            .where(
                OperatorSessionModel.id != candidate.id,
                OperatorSessionModel.csrf_hash == candidate.csrf_hash,
            )
            .with_for_update()
        )
        if hash_owner is not None:
            raise SessionConflict("CSRF rotation token hash conflicts")
        row.csrf_hash = candidate.csrf_hash
        row.last_seen_at = candidate.last_seen_at
        row.version = candidate.version
        return candidate

    async def revoke_all_active(self, *, revoked_at: datetime) -> tuple[OperatorSession, ...]:
        rows = tuple(
            await self._session.scalars(
                select(OperatorSessionModel)
                .where(OperatorSessionModel.revoked_at.is_(None))
                .order_by(OperatorSessionModel.id)
                .with_for_update()
            )
        )
        revoked: list[OperatorSession] = []
        for row in rows:
            updated = _session_from_row(row).revoke(revoked_at)
            row.revoked_at = updated.revoked_at
            row.version = updated.version
            revoked.append(updated)
        return tuple(revoked)

    async def login_state(self, *, observed_at: datetime) -> OperatorAuthState:
        return _auth_state_from_row(await self._locked_auth_state(observed_at))

    async def require_login_allowed(self, *, observed_at: datetime) -> OperatorAuthState:
        current = await self.login_state(observed_at=observed_at)
        if current.locked(observed_at):
            raise LoginAuthenticationError
        return current

    async def record_login_failure(self, *, observed_at: datetime) -> OperatorAuthState:
        row = await self._locked_auth_state(observed_at)
        current = _auth_state_from_row(row)
        if observed_at < current.updated_at:
            raise ValueError("login observation cannot precede durable throttle state")
        if current.locked(observed_at):
            return current
        if (
            current.window_started_at is None
            or observed_at >= current.window_started_at + LOGIN_WINDOW
        ):
            failed_attempts = 1
            window_started_at = observed_at
        else:
            failed_attempts = current.failed_attempts + 1
            window_started_at = current.window_started_at
        locked_until = (
            observed_at + LOGIN_LOCKOUT if failed_attempts >= LOGIN_MAX_FAILURES else None
        )
        updated = OperatorAuthState(
            failed_attempts=failed_attempts,
            window_started_at=window_started_at,
            locked_until=locked_until,
            updated_at=observed_at,
            version=current.version + 1,
        )
        _write_auth_state(row, updated)
        return updated

    async def record_login_success(self, *, observed_at: datetime) -> OperatorAuthState:
        row = await self._locked_auth_state(observed_at)
        current = _auth_state_from_row(row)
        if observed_at < current.updated_at:
            raise ValueError("login observation cannot precede durable throttle state")
        if current.locked(observed_at):
            raise LoginAuthenticationError
        updated = OperatorAuthState(
            failed_attempts=0,
            window_started_at=None,
            locked_until=None,
            updated_at=observed_at,
            version=current.version + 1,
        )
        _write_auth_state(row, updated)
        return updated

    async def _locked_session(self, session_id: UUID) -> OperatorSessionModel:
        row = await self._session.scalar(
            select(OperatorSessionModel)
            .where(OperatorSessionModel.id == session_id)
            .with_for_update()
        )
        if row is None:
            raise SessionAuthenticationError
        return row

    async def _locked_auth_state(self, observed_at: datetime) -> OperatorAuthStateModel:
        _require_utc(observed_at)
        await self._session.execute(
            insert(OperatorAuthStateModel)
            .values(
                id=1,
                failed_attempts=0,
                window_started_at=None,
                locked_until=None,
                updated_at=observed_at,
                version=1,
            )
            .on_conflict_do_nothing(index_elements=[OperatorAuthStateModel.id])
        )
        row = await self._session.scalar(
            select(OperatorAuthStateModel).where(OperatorAuthStateModel.id == 1).with_for_update()
        )
        if row is None:  # pragma: no cover - guarded by insert and database constraint
            raise RuntimeError("operator authentication singleton is missing")
        return row


def _session_values(session: OperatorSession) -> dict[str, object]:
    return {
        "id": session.id,
        "token_hash": session.token_hash,
        "csrf_hash": session.csrf_hash,
        "actor": session.actor,
        "created_at": session.created_at,
        "last_seen_at": session.last_seen_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "version": session.version,
    }


def _session_from_row(row: OperatorSessionModel) -> OperatorSession:
    return OperatorSession(
        id=row.id,
        token_hash=row.token_hash,
        csrf_hash=row.csrf_hash,
        actor=row.actor,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        version=row.version,
    )


def _auth_state_from_row(row: OperatorAuthStateModel) -> OperatorAuthState:
    return OperatorAuthState(
        failed_attempts=row.failed_attempts,
        window_started_at=row.window_started_at,
        locked_until=row.locked_until,
        updated_at=row.updated_at,
        version=row.version,
    )


def _write_auth_state(row: OperatorAuthStateModel, state: OperatorAuthState) -> None:
    row.failed_attempts = state.failed_attempts
    row.window_started_at = state.window_started_at
    row.locked_until = state.locked_until
    row.updated_at = state.updated_at
    row.version = state.version


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("login observation must be timezone-aware UTC")
