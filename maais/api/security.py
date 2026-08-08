"""Mission Control authentication dependencies without domain mutation authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from secrets import compare_digest
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.config.security import AuthMode, SecuritySettings
from maais.db.connection import get_session_factory
from maais.db.unit_of_work import UnitOfWork
from maais.security.sessions import (
    OperatorSession,
    SessionAuthenticationError,
    opaque_token_hash,
    verify_csrf_token,
)

SessionFactory = async_sessionmaker[AsyncSession]
SESSION_COOKIE_NAME = "__Host-maais_session"
SESSION_AUTHENTICATION_REQUIRED = "session_authentication_required"
CSRF_VERIFICATION_FAILED = "csrf_verification_failed"
ORIGIN_VERIFICATION_FAILED = "origin_verification_failed"


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    actor: str
    session_id: UUID | None
    auth_mode: AuthMode


@dataclass(frozen=True, slots=True)
class MissionControlSecurity:
    settings: SecuritySettings
    control_token: str | None
    clock: Callable[[], datetime]


def security_context(request: Request) -> MissionControlSecurity:
    context = request.app.state.security
    if not isinstance(context, MissionControlSecurity):  # pragma: no cover - app invariant
        raise RuntimeError("Mission Control security context is not configured")
    return context


def session_factory(request: Request) -> SessionFactory:
    factory: SessionFactory | None = request.app.state.session_factory
    if factory is None:
        factory = get_session_factory()
        request.app.state.session_factory = factory
    return factory


async def require_operator(request: Request) -> OperatorPrincipal:
    context = security_context(request)
    if context.settings.auth_mode is AuthMode.LOCAL_TOKEN:
        supplied = _bearer_token(request.headers.get("Authorization"))
        if (
            context.control_token is None
            or not supplied
            or not compare_digest(supplied, context.control_token)
        ):
            raise HTTPException(
                status_code=401,
                detail="valid local control bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return OperatorPrincipal(
            actor="local_operator",
            session_id=None,
            auth_mode=AuthMode.LOCAL_TOKEN,
        )
    if request.headers.get("Authorization") is not None:
        raise _session_required()
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    try:
        token_hash = opaque_token_hash(token, context.settings.session_pepper)
    except ValueError:
        token_hash = "0" * 64
    try:
        async with UnitOfWork(session_factory(request)).begin() as uow:
            authenticated = await uow.sessions.authenticate(
                token_hash,
                observed_at=context.clock(),
            )
    except SessionAuthenticationError as error:
        raise _session_required() from error
    request.state.operator_session = authenticated
    return OperatorPrincipal(
        actor=authenticated.actor,
        session_id=authenticated.id,
        auth_mode=AuthMode.OPERATOR_SESSION,
    )


async def optional_operator_session(request: Request) -> OperatorSession | None:
    context = security_context(request)
    if context.settings.auth_mode is not AuthMode.OPERATOR_SESSION:
        return None
    if request.headers.get("Authorization") is not None:
        return None
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    try:
        token_hash = opaque_token_hash(token, context.settings.session_pepper)
    except ValueError:
        token_hash = "0" * 64
    try:
        async with UnitOfWork(session_factory(request)).begin() as uow:
            return await uow.sessions.authenticate(
                token_hash,
                observed_at=context.clock(),
            )
    except SessionAuthenticationError:
        return None


async def require_csrf(
    request: Request,
    principal: OperatorPrincipal = Depends(require_operator),
) -> OperatorPrincipal:
    if principal.auth_mode is AuthMode.LOCAL_TOKEN:
        return principal
    require_same_origin(request)
    session = getattr(request.state, "operator_session", None)
    context = security_context(request)
    presented = request.headers.get("X-CSRF-Token", "")
    if not isinstance(session, OperatorSession) or not verify_csrf_token(
        presented,
        session.csrf_hash,
        context.settings.csrf_pepper,
    ):
        raise HTTPException(status_code=403, detail=CSRF_VERIFICATION_FAILED)
    return principal


def require_same_origin(request: Request) -> None:
    context = security_context(request)
    if context.settings.auth_mode is AuthMode.LOCAL_TOKEN:
        return
    if (
        request.headers.get("Origin") != context.settings.public_origin
        or request.headers.get("Host") != context.settings.public_host
    ):
        raise HTTPException(status_code=403, detail=ORIGIN_VERIFICATION_FAILED)


def set_session_cookie(
    response: Response,
    *,
    token: str,
    expires_at: datetime,
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=43_200,
        expires=expires_at,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _bearer_token(authorization: str | None) -> str:
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        return ""
    return authorization[len(prefix) :]


def _session_required() -> HTTPException:
    return HTTPException(status_code=401, detail=SESSION_AUTHENTICATION_REQUIRED)
