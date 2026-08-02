from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.api.queries import MissionControlQueryService
from maais.api.schemas import (
    ApiHealth,
    DecisionDetail,
    DecisionPage,
    ExperimentListItem,
    ExperimentOverview,
)
from maais.db.connection import get_engine, get_session_factory

SessionFactory = async_sessionmaker[AsyncSession]


def create_app(
    session_factory: SessionFactory | None = None,
    *,
    dashboard_dir: Path | None = None,
) -> FastAPI:
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
        description="Read-only local paper-trading operations and audit API.",
        lifespan=lifespan,
    )
    application.state.session_factory = session_factory
    application.add_middleware(
        CORSMiddleware,
        allow_origins=("http://127.0.0.1:5173", "http://localhost:5173"),
        allow_credentials=False,
        allow_methods=("GET",),
        allow_headers=("Accept", "Content-Type"),
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

    @application.get("/api/v1/decisions/{decision_id}", response_model=DecisionDetail)
    async def decision(
        decision_id: UUID,
        session: AsyncSession = Depends(read_session),
    ) -> DecisionDetail:
        return await MissionControlQueryService(session).get_decision(decision_id)

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


app = create_app()
