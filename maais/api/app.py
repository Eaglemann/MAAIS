from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from secrets import compare_digest
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.api.auth import load_control_token
from maais.api.queries import MissionControlQueryService
from maais.api.schemas import (
    ApiHealth,
    DecisionDetail,
    DecisionPage,
    ExperimentListItem,
    ExperimentOverview,
    OperatorCommandPage,
    OperatorCommandRequest,
    OperatorCommandView,
    OutboxCursorEvent,
    OutboxCursorPage,
    ResearchLabView,
    TradePage,
)
from maais.config.settings import get_settings
from maais.db.connection import get_engine, get_session_factory
from maais.db.models.experiments import ExperimentModel
from maais.db.models.ledger import OutboxEventModel
from maais.db.repositories.events import EventRepository
from maais.db.repositories.operator_commands import (
    OperatorCommandConflict,
    OperatorCommandRepository,
)
from maais.db.unit_of_work import UnitOfWork
from maais.operations.operator_commands import CommandStatus, OperatorCommand

SessionFactory = async_sessionmaker[AsyncSession]


def create_app(
    session_factory: SessionFactory | None = None,
    *,
    dashboard_dir: Path | None = None,
    control_token: str | None = None,
    control_token_file: Path | None = None,
) -> FastAPI:
    if control_token is not None and control_token_file is not None:
        raise ValueError("provide either a control token or token file, not both")
    if control_token is not None:
        if len(control_token) < 32:
            raise ValueError("direct Mission Control token must be at least 32 characters")
        if control_token != control_token.strip():
            raise ValueError("direct Mission Control token must be trimmed")
    resolved_control_token = control_token
    if resolved_control_token is None:
        resolved_control_token = load_control_token(
            control_token_file or get_settings().mission_control_token_file
        )
    owns_global_engine = session_factory is None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if application.state.session_factory is None:
            application.state.session_factory = get_session_factory()
        yield
        if owns_global_engine:
            await get_engine().dispose()

    application = FastAPI(
        title="MAAIS Mission Control",
        version="0.1.0",
        description="Local paper-trading operations, audit, and queued control API.",
        lifespan=lifespan,
    )
    application.state.session_factory = session_factory
    application.add_middleware(
        CORSMiddleware,
        allow_origins=("http://127.0.0.1:5173", "http://localhost:5173"),
        allow_credentials=False,
        allow_methods=("GET", "POST"),
        allow_headers=("Accept", "Authorization", "Content-Type"),
    )

    async def read_session() -> AsyncIterator[AsyncSession]:
        factory: SessionFactory | None = application.state.session_factory
        if factory is None:
            factory = get_session_factory()
            application.state.session_factory = factory
        async with factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                yield session

    async def require_control_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        prefix = "Bearer "
        supplied = (
            authorization[len(prefix) :]
            if authorization is not None and authorization.startswith(prefix)
            else ""
        )
        if (
            resolved_control_token is None
            or not supplied
            or not compare_digest(supplied, resolved_control_token)
        ):
            raise HTTPException(
                status_code=401,
                detail="valid local control bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @application.middleware("http")
    async def disable_browser_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(LookupError)
    async def not_found(_request: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.exception_handler(ValueError)
    async def invalid_query(_request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @application.exception_handler(OperatorCommandConflict)
    async def command_conflict(
        _request: Request,
        error: OperatorCommandConflict,
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.get("/api/v1/health", response_model=ApiHealth)
    async def health(session: AsyncSession = Depends(read_session)) -> ApiHealth:
        transaction_mode = str(await session.scalar(text("SHOW transaction_read_only")))
        schema_revision = str(await session.scalar(text("SELECT version_num FROM alembic_version")))
        return ApiHealth(
            service="maais-mission-control",
            status="ok",
            database_transaction=("read only" if transaction_mode == "on" else transaction_mode),
            schema_revision=schema_revision,
            checked_at=datetime.now(timezone.utc),
        )

    @application.get("/api/v1/experiments", response_model=tuple[ExperimentListItem, ...])
    async def experiments(
        limit: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(read_session),
    ) -> tuple[ExperimentListItem, ...]:
        return await MissionControlQueryService(session).list_experiments(limit=limit)

    @application.get(
        "/api/v1/experiments/{experiment_id}/overview",
        response_model=ExperimentOverview,
    )
    async def overview(
        experiment_id: UUID,
        session: AsyncSession = Depends(read_session),
    ) -> ExperimentOverview:
        return await MissionControlQueryService(session).get_overview(experiment_id)

    @application.get(
        "/api/v1/experiments/{experiment_id}/decisions",
        response_model=DecisionPage,
    )
    async def decisions(
        experiment_id: UUID,
        symbol: str | None = None,
        status: str | None = None,
        disposition: str | None = None,
        reason_code: str | None = None,
        before_at: datetime | None = None,
        before_id: UUID | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: AsyncSession = Depends(read_session),
    ) -> DecisionPage:
        return await MissionControlQueryService(session).list_decisions(
            experiment_id,
            symbol=symbol,
            status=status,
            disposition=disposition,
            reason_code=reason_code,
            before_at=before_at,
            before_id=before_id,
            limit=limit,
        )

    @application.get(
        "/api/v1/experiments/{experiment_id}/trades",
        response_model=TradePage,
    )
    async def trades(
        experiment_id: UUID,
        symbol: str | None = None,
        proposal_status: str | None = None,
        decision_disposition: str | None = None,
        before_at: datetime | None = None,
        before_id: UUID | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: AsyncSession = Depends(read_session),
    ) -> TradePage:
        return await MissionControlQueryService(session).list_trades(
            experiment_id,
            symbol=symbol,
            proposal_status=proposal_status,
            decision_disposition=decision_disposition,
            before_at=before_at,
            before_id=before_id,
            limit=limit,
        )

    @application.get("/api/v1/decisions/{decision_id}", response_model=DecisionDetail)
    async def decision(
        decision_id: UUID,
        session: AsyncSession = Depends(read_session),
    ) -> DecisionDetail:
        return await MissionControlQueryService(session).get_decision(decision_id)

    @application.get(
        "/api/v1/experiments/{experiment_id}/research",
        response_model=ResearchLabView,
    )
    async def research(
        experiment_id: UUID,
        limit_per_kind: int = Query(500, ge=1, le=1000),
        session: AsyncSession = Depends(read_session),
    ) -> ResearchLabView:
        return await MissionControlQueryService(session).get_research_lab(
            experiment_id,
            limit_per_kind=limit_per_kind,
        )

    @application.post(
        "/api/v1/experiments/{experiment_id}/commands",
        response_model=OperatorCommandView,
        status_code=202,
    )
    async def request_command(
        experiment_id: UUID,
        request: OperatorCommandRequest,
        _authorized: None = Depends(require_control_token),
    ) -> OperatorCommandView:
        del _authorized
        factory: SessionFactory | None = application.state.session_factory
        if factory is None:
            factory = get_session_factory()
            application.state.session_factory = factory
        command = OperatorCommand.request(
            command_id=uuid4(),
            experiment_id=experiment_id,
            command_type=request.command_type,
            idempotency_key=request.idempotency_key,
            actor="local_operator",
            reason=request.reason,
            payload=request.payload,
            confirmation=request.confirmation,
            requested_at=datetime.now(timezone.utc),
        )
        async with UnitOfWork(factory).begin() as uow:
            await uow.experiments.get_status(experiment_id)
            recorded = await uow.commands.enqueue(command)
        return OperatorCommandView.model_validate(recorded.command.to_dict())

    @application.get(
        "/api/v1/experiments/{experiment_id}/commands",
        response_model=OperatorCommandPage,
    )
    async def commands(
        experiment_id: UUID,
        status: CommandStatus | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: AsyncSession = Depends(read_session),
    ) -> OperatorCommandPage:
        exists = await session.scalar(
            select(ExperimentModel.id).where(ExperimentModel.id == experiment_id)
        )
        if exists is None:
            raise LookupError(f"experiment not found: {experiment_id}")
        repository = OperatorCommandRepository(session, EventRepository(session))
        items = await repository.list_for_experiment(
            experiment_id,
            status=status,
            limit=limit,
        )
        return OperatorCommandPage(
            items=tuple(OperatorCommandView.model_validate(command.to_dict()) for command in items),
            limit=limit,
        )

    @application.get(
        "/api/v1/commands/{command_id}",
        response_model=OperatorCommandView,
    )
    async def command(
        command_id: UUID,
        session: AsyncSession = Depends(read_session),
    ) -> OperatorCommandView:
        repository = OperatorCommandRepository(session, EventRepository(session))
        stored = await repository.get(command_id)
        return OperatorCommandView.model_validate(stored.to_dict())

    @application.get("/api/v1/events", response_model=OutboxCursorPage)
    async def events(
        after_cursor: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=1000),
        session: AsyncSession = Depends(read_session),
    ) -> OutboxCursorPage:
        return await _outbox_cursor_page(
            session,
            after_cursor=after_cursor,
            limit=limit,
        )

    @application.websocket("/api/v1/events/stream")
    async def event_stream(
        websocket: WebSocket,
        after_cursor: int = 0,
    ) -> None:
        if after_cursor < 0:
            await websocket.close(code=1008, reason="after_cursor cannot be negative")
            return
        await websocket.accept()
        cursor = after_cursor
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        try:
            while True:
                factory: SessionFactory | None = application.state.session_factory
                if factory is None:
                    factory = get_session_factory()
                    application.state.session_factory = factory
                async with factory() as session:
                    async with session.begin():
                        await session.execute(text("SET TRANSACTION READ ONLY"))
                        page = await _outbox_cursor_page(
                            session,
                            after_cursor=cursor,
                            limit=500,
                        )
                if page.items:
                    cursor = page.next_cursor
                    await websocket.send_json({"type": "events", **page.model_dump(mode="json")})
                elif loop.time() - last_heartbeat >= 10:
                    await websocket.send_json(
                        {
                            "type": "heartbeat",
                            "next_cursor": cursor,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    last_heartbeat = loop.time()
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return

    resolved_dashboard = dashboard_dir or Path(__file__).resolve().parents[2] / "dashboard" / "dist"
    if resolved_dashboard.is_dir():
        application.mount(
            "/",
            StaticFiles(directory=resolved_dashboard, html=True),
            name="mission-control-dashboard",
        )
    else:

        @application.get("/", include_in_schema=False)
        async def dashboard_not_built() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Mission Control dashboard is not built; run npm run build in dashboard"
                    )
                },
            )

    return application


async def _outbox_cursor_page(
    session: AsyncSession,
    *,
    after_cursor: int,
    limit: int,
) -> OutboxCursorPage:
    high_watermark = int(await session.scalar(select(func.max(OutboxEventModel.cursor))) or 0)
    effective_after = min(after_cursor, high_watermark)
    rows = (
        await session.scalars(
            select(OutboxEventModel)
            .where(OutboxEventModel.cursor > effective_after)
            .order_by(OutboxEventModel.cursor)
            .limit(limit + 1)
        )
    ).all()
    visible = rows[:limit]
    items = tuple(
        OutboxCursorEvent(
            cursor=row.cursor,
            event_type=row.topic,
            created_at=row.created_at,
            payload=row.payload_json,
        )
        for row in visible
    )
    return OutboxCursorPage(
        items=items,
        limit=limit,
        has_more=len(rows) > limit,
        next_cursor=items[-1].cursor if items else effective_after,
    )


app = create_app()
