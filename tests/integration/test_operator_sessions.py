from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr
from sqlalchemy import CheckConstraint, func, insert, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from maais.db.connection import Base
from maais.db.models.auth import OperatorAuthStateModel, OperatorSessionModel
from maais.db.repositories.sessions import LoginAuthenticationError, SessionConflict
from maais.db.unit_of_work import UnitOfWork
from maais.security.sessions import (
    INVALID_SESSION,
    LOGIN_MAX_FAILURES,
    SessionAuthenticationError,
    issue_session_tokens,
    rotate_session_tokens,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
SESSION_PEPPER = SecretStr(
    "session-pepper-integration-0123456789-ABCDEFG"  # pragma: allowlist secret
)
CSRF_PEPPER = SecretStr(
    "csrf-pepper-integration-0123456789-ABCDEFGHIJ"  # pragma: allowlist secret
)
EXPECTED_COLUMNS = {
    "operator_sessions": (
        "id",
        "token_hash",
        "csrf_hash",
        "actor",
        "created_at",
        "last_seen_at",
        "expires_at",
        "revoked_at",
        "version",
    ),
    "operator_auth_state": (
        "id",
        "failed_attempts",
        "window_started_at",
        "locked_until",
        "updated_at",
        "version",
    ),
}


def _issued(*, observed_at: datetime = NOW):
    return issue_session_tokens(
        actor="sole_operator",
        observed_at=observed_at,
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )


async def test_auth_model_and_database_contract_are_exact(
    db_connection: AsyncConnection,
) -> None:
    assert Base.metadata.tables["maais_auth.operator_sessions"] is OperatorSessionModel.__table__
    assert (
        Base.metadata.tables["maais_auth.operator_auth_state"] is OperatorAuthStateModel.__table__
    )
    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables[f"maais_auth.{table_name}"]
        assert tuple(column.name for column in table.columns) == expected_columns
        assert {
            str(constraint.name)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }

    def compare(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        assert {"operator_sessions", "operator_auth_state"} <= set(
            inspector.get_table_names(schema="maais_auth")
        )
        for table_name, expected_columns in EXPECTED_COLUMNS.items():
            assert (
                tuple(
                    column["name"]
                    for column in inspector.get_columns(table_name, schema="maais_auth")
                )
                == expected_columns
            )
            assert {
                item["name"]
                for item in inspector.get_check_constraints(
                    table_name,
                    schema="maais_auth",
                )
            } == {
                str(constraint.name)
                for constraint in Base.metadata.tables[f"maais_auth.{table_name}"].constraints
                if isinstance(constraint, CheckConstraint)
            }

    await db_connection.run_sync(compare)
    assert await db_connection.scalar(select(func.count()).select_from(OperatorAuthStateModel)) == 1


async def test_session_issue_authenticate_rotate_and_revoke_are_row_locked(
    uow_factory: UnitOfWork,
) -> None:
    issued = _issued()
    async with uow_factory.begin() as uow:
        stored = await uow.sessions.issue(issued.to_request())
        authenticated = await uow.sessions.authenticate(
            issued.session.token_hash,
            observed_at=NOW + timedelta(minutes=1),
        )
    rotated = rotate_session_tokens(
        authenticated,
        observed_at=NOW + timedelta(minutes=2),
        session_pepper=SESSION_PEPPER,
        csrf_pepper=CSRF_PEPPER,
    )
    async with uow_factory.begin() as uow:
        stored_rotated = await uow.sessions.rotate(rotated.session)
        with pytest.raises(SessionAuthenticationError):
            await uow.sessions.authenticate(
                issued.session.token_hash,
                observed_at=NOW + timedelta(minutes=3),
            )
        authenticated_rotated = await uow.sessions.authenticate(
            rotated.session.token_hash,
            observed_at=NOW + timedelta(minutes=3),
        )
        revoked = await uow.sessions.revoke(
            rotated.session.id,
            revoked_at=NOW + timedelta(minutes=4),
        )
        repeated = await uow.sessions.revoke(
            rotated.session.id,
            revoked_at=NOW + timedelta(minutes=5),
        )

    assert stored == issued.session
    assert stored_rotated.version == 3
    assert authenticated_rotated.last_seen_at == NOW + timedelta(minutes=3)
    assert repeated == revoked
    assert repeated.revoked_at == NOW + timedelta(minutes=4)


async def test_session_hashes_are_unique_and_conflicts_are_explicit(
    uow_factory: UnitOfWork,
) -> None:
    issued = _issued()
    collision = _issued()
    collision = replace(
        collision,
        session=replace(collision.session, token_hash=issued.session.token_hash),
    )
    async with uow_factory.begin() as uow:
        await uow.sessions.issue(issued.to_request())
        with pytest.raises(SessionConflict, match="hash"):
            await uow.sessions.issue(collision.to_request())


async def test_database_rejects_extended_ttl_and_unlocked_fifth_failure(
    uow_factory: UnitOfWork,
) -> None:
    issued = _issued()
    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                insert(OperatorSessionModel).values(
                    id=issued.session.id,
                    token_hash=issued.session.token_hash,
                    csrf_hash=issued.session.csrf_hash,
                    actor=issued.session.actor,
                    created_at=issued.session.created_at,
                    last_seen_at=issued.session.last_seen_at,
                    expires_at=issued.session.expires_at + timedelta(seconds=1),
                    revoked_at=None,
                    version=1,
                )
            )
    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                update(OperatorAuthStateModel)
                .where(OperatorAuthStateModel.id == 1)
                .values(
                    failed_attempts=5,
                    window_started_at=NOW,
                    locked_until=None,
                    updated_at=NOW,
                    version=2,
                )
            )


@pytest.mark.parametrize("failure_kind", ("missing", "expired_absolute", "expired_idle"))
async def test_missing_and_expired_sessions_have_one_public_failure(
    uow_factory: UnitOfWork,
    failure_kind: str,
) -> None:
    issued = _issued(observed_at=NOW - timedelta(hours=1))
    token_hash = "f" * 64
    observed_at = NOW
    if failure_kind != "missing":
        async with uow_factory.begin() as uow:
            await uow.sessions.issue(issued.to_request())
        token_hash = issued.session.token_hash
        if failure_kind == "expired_absolute":
            observed_at = issued.session.expires_at + timedelta(microseconds=1)

    async with uow_factory.begin() as uow:
        with pytest.raises(SessionAuthenticationError) as error:
            await uow.sessions.authenticate(token_hash, observed_at=observed_at)

    assert error.value.public_error_code == INVALID_SESSION
    assert str(error.value) == "session authentication failed"


async def test_global_login_throttle_serializes_failures_lockout_and_success_reset(
    uow_factory: UnitOfWork,
) -> None:
    async def fail_once() -> None:
        async with uow_factory.begin() as uow:
            await uow.sessions.record_login_failure(observed_at=NOW)

    await asyncio.gather(*(fail_once() for _ in range(LOGIN_MAX_FAILURES)))
    async with uow_factory.begin() as uow:
        locked = await uow.sessions.login_state(observed_at=NOW)
        with pytest.raises(LoginAuthenticationError) as error:
            await uow.sessions.require_login_allowed(observed_at=NOW)
        reset = await uow.sessions.record_login_success(
            observed_at=locked.locked_until + timedelta(microseconds=1),  # type: ignore[operator]
        )

    assert locked.failed_attempts == LOGIN_MAX_FAILURES
    assert locked.locked_until == NOW + timedelta(minutes=30)
    assert error.value.public_error_code == "invalid_credentials"
    assert reset.failed_attempts == 0
    assert reset.window_started_at is None
    assert reset.locked_until is None


async def test_failure_window_expires_before_next_count(
    uow_factory: UnitOfWork,
) -> None:
    async with uow_factory.begin() as uow:
        first = await uow.sessions.record_login_failure(observed_at=NOW)
    async with uow_factory.begin() as uow:
        reset_window = await uow.sessions.record_login_failure(
            observed_at=NOW + timedelta(minutes=15, microseconds=1)
        )

    assert first.failed_attempts == 1
    assert reset_window.failed_attempts == 1
    assert reset_window.window_started_at == NOW + timedelta(minutes=15, microseconds=1)


async def test_concurrent_logout_and_authentication_never_resurrect_session(
    uow_factory: UnitOfWork,
) -> None:
    issued = _issued()
    async with uow_factory.begin() as uow:
        await uow.sessions.issue(issued.to_request())

    async def authenticate() -> None:
        try:
            async with uow_factory.begin() as uow:
                await uow.sessions.authenticate(
                    issued.session.token_hash,
                    observed_at=NOW + timedelta(minutes=1),
                )
        except SessionAuthenticationError:
            pass

    async def revoke() -> None:
        async with uow_factory.begin() as uow:
            await uow.sessions.revoke(
                issued.session.id,
                revoked_at=NOW + timedelta(minutes=1),
            )

    await asyncio.gather(authenticate(), revoke())
    async with uow_factory.begin() as uow:
        with pytest.raises(SessionAuthenticationError):
            await uow.sessions.authenticate(
                issued.session.token_hash,
                observed_at=NOW + timedelta(minutes=2),
            )
        count = await uow.session.scalar(select(func.count()).select_from(OperatorSessionModel))

    assert count == 1
