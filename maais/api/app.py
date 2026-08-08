from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.api.auth import load_control_token
from maais.api.queries import MissionControlQueryService
from maais.api.schemas import (
    ApiHealth,
    AuthSessionView,
    CsrfTokenResponse,
    DecisionDetail,
    DecisionListItem,
    DecisionPage,
    ExperimentListItem,
    ExperimentOverview,
    LoginRequest,
    LoginResponse,
    OperatorCommandPage,
    OperatorCommandRequest,
    OperatorCommandView,
    OutboxCursorEvent,
    OutboxCursorPage,
    ResearchLabView,
    TradeListItem,
    TradePage,
)
from maais.api.security import (
    MissionControlSecurity,
    OperatorPrincipal,
    clear_session_cookie,
    optional_operator_session,
    require_csrf,
    require_operator,
    require_same_origin,
    security_context,
    set_session_cookie,
)
from maais.api.security import (
    session_factory as security_session_factory,
)
from maais.config.security import AuthMode, SecuritySettings
from maais.config.settings import get_settings
from maais.db.connection import get_engine, get_session_factory
from maais.db.models.experiments import ExperimentModel
from maais.db.models.ledger import OutboxEventModel
from maais.db.repositories.events import EventRepository
from maais.db.repositories.operator_commands import (
    OperatorCommandConflict,
    OperatorCommandRepository,
)
from maais.db.repositories.sessions import LoginAuthenticationError
from maais.db.unit_of_work import UnitOfWork
from maais.operations.operator_commands import CommandStatus, OperatorCommand
from maais.operations.verification import establish_read_only_snapshot
from maais.security.passwords import INVALID_CREDENTIALS, verify_operator_password
from maais.security.sessions import issue_session_tokens, rotate_csrf_token

SessionFactory = async_sessionmaker[AsyncSession]

_DECISION_CSV_COLUMNS = (
    "decision_id",
    "experiment_id",
    "cycle_at",
    "symbol",
    "timeframe",
    "regime",
    "strategy_version_id",
    "status",
    "direction",
    "disposition",
    "reason_code",
    "outcome",
    "quality_status",
    "consensus_direction",
    "consensus_probability",
    "consensus_confidence",
    "proposal_status",
    "order_status",
    "counterfactual_status",
    "market_frame_id",
    "created_at",
    "completed_at",
)

_TRADE_CSV_COLUMNS = (
    "proposal_id",
    "decision_cycle_id",
    "proposed_at",
    "latest_activity_at",
    "symbol",
    "direction",
    "regime",
    "strategy_version_id",
    "proposal_status",
    "proposal_reason_code",
    "decision_disposition",
    "decision_reason_code",
    "outcome",
    "approved_notional",
    "official_order_count",
    "order_statuses",
    "fill_count",
    "filled_quantity",
    "gross_fill_notional",
    "fees",
    "total_slippage",
    "counterfactual_status",
    "counterfactual_pnl",
)


def _decision_csv_chunk(
    items: tuple[DecisionListItem, ...],
    *,
    include_header: bool,
) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_DECISION_CSV_COLUMNS)
    if include_header:
        writer.writeheader()
    for item in items:
        row = item.model_dump(mode="json")
        row["decision_id"] = row.pop("id")
        writer.writerow({column: row.get(column) for column in _DECISION_CSV_COLUMNS})
    return output.getvalue()


def _trade_csv_chunk(
    items: tuple[TradeListItem, ...],
    *,
    include_header: bool,
) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_TRADE_CSV_COLUMNS)
    if include_header:
        writer.writeheader()
    for item in items:
        row = item.model_dump(mode="json")
        row["order_statuses"] = "|".join(item.order_statuses)
        writer.writerow({column: row.get(column) for column in _TRADE_CSV_COLUMNS})
    return output.getvalue()


def create_app(
    session_factory: SessionFactory | None = None,
    *,
    dashboard_dir: Path | None = None,
    control_token: str | None = None,
    control_token_file: Path | None = None,
    security_settings: SecuritySettings | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    if control_token is not None and control_token_file is not None:
        raise ValueError("provide either a control token or token file, not both")
    if control_token is not None:
        if len(control_token) < 32:
            raise ValueError("direct Mission Control token must be at least 32 characters")
        if control_token != control_token.strip():
            raise ValueError("direct Mission Control token must be trimmed")
    global_settings = None
    resolved_security = security_settings
    if resolved_security is None:
        global_settings = get_settings()
        resolved_security = global_settings.security
    if resolved_security.auth_mode is AuthMode.OPERATOR_SESSION and (
        control_token is not None or control_token_file is not None
    ):
        raise ValueError("operator session mode forbids local control token configuration")
    resolved_control_token = None
    if resolved_security.auth_mode is AuthMode.LOCAL_TOKEN:
        resolved_control_token = control_token
        if resolved_control_token is None:
            token_path = control_token_file
            if token_path is None and global_settings is not None:
                token_path = global_settings.mission_control_token_file
            resolved_control_token = load_control_token(token_path)
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
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
    application.state.security = MissionControlSecurity(
        settings=resolved_security,
        control_token=resolved_control_token,
        clock=resolved_clock,
    )
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
                await establish_read_only_snapshot(session)
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

    @application.exception_handler(OperatorCommandConflict)
    async def command_conflict(
        _request: Request,
        error: OperatorCommandConflict,
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.post("/api/v1/auth/login", response_model=LoginResponse)
    async def login(http_request: Request, response: Response) -> LoginResponse:
        context = security_context(http_request)
        if context.settings.auth_mode is not AuthMode.OPERATOR_SESSION:
            raise HTTPException(status_code=404, detail="operator session login is not enabled")
        try:
            payload = await http_request.json()
            login_request = LoginRequest.model_validate(payload)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="invalid_login_payload") from error
        observed_at = context.clock()
        issued = None
        async with UnitOfWork(security_session_factory(http_request)).begin() as uow:
            blocked = False
            try:
                await uow.sessions.require_login_allowed(observed_at=observed_at)
            except LoginAuthenticationError:
                blocked = True
            if not blocked:
                verification = await asyncio.to_thread(
                    verify_operator_password,
                    login_request.password,
                    context.settings.operator_password_hash_value,
                )
                if not verification.valid:
                    await uow.sessions.record_login_failure(observed_at=observed_at)
                else:
                    await uow.sessions.record_login_success(observed_at=observed_at)
                    await uow.sessions.revoke_all_active(revoked_at=observed_at)
                    issued = issue_session_tokens(
                        actor="sole_operator",
                        observed_at=observed_at,
                        session_pepper=context.settings.session_pepper,
                        csrf_pepper=context.settings.csrf_pepper,
                    )
                    await uow.sessions.issue(issued.to_request())
        if issued is None:
            raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)
        set_session_cookie(
            response,
            token=issued.token,
            expires_at=issued.session.expires_at,
        )
        return LoginResponse(
            actor=issued.session.actor,
            auth_mode=AuthMode.OPERATOR_SESSION,
            csrf_token=issued.csrf_token,
            expires_at=issued.session.expires_at,
        )

    @application.get("/api/v1/auth/session", response_model=AuthSessionView)
    async def auth_session(http_request: Request, response: Response) -> AuthSessionView:
        context = security_context(http_request)
        authenticated = await optional_operator_session(http_request)
        if authenticated is None:
            if http_request.cookies:
                clear_session_cookie(response)
            return AuthSessionView(
                authenticated=False,
                actor=None,
                auth_mode=context.settings.auth_mode,
                expires_at=None,
            )
        return AuthSessionView(
            authenticated=True,
            actor=authenticated.actor,
            auth_mode=AuthMode.OPERATOR_SESSION,
            expires_at=authenticated.expires_at,
        )

    @application.post("/api/v1/auth/csrf", response_model=CsrfTokenResponse)
    async def csrf_bootstrap(
        http_request: Request,
        principal: OperatorPrincipal = Depends(require_operator),
    ) -> CsrfTokenResponse:
        if principal.auth_mode is not AuthMode.OPERATOR_SESSION:
            raise HTTPException(status_code=404, detail="operator session CSRF is not enabled")
        require_same_origin(http_request)
        context = security_context(http_request)
        current = http_request.state.operator_session
        issued = rotate_csrf_token(
            current,
            observed_at=context.clock(),
            csrf_pepper=context.settings.csrf_pepper,
        )
        async with UnitOfWork(security_session_factory(http_request)).begin() as uow:
            await uow.sessions.rotate_csrf(issued.session)
        return CsrfTokenResponse(csrf_token=issued.csrf_token)

    @application.post("/api/v1/auth/logout", status_code=204)
    async def logout(
        http_request: Request,
        principal: OperatorPrincipal = Depends(require_csrf),
    ) -> Response:
        response = Response(status_code=204)
        if principal.auth_mode is AuthMode.OPERATOR_SESSION:
            assert principal.session_id is not None
            context = security_context(http_request)
            async with UnitOfWork(security_session_factory(http_request)).begin() as uow:
                await uow.sessions.revoke(
                    principal.session_id,
                    revoked_at=context.clock(),
                )
            clear_session_cookie(response)
        return response

    @application.get("/api/v1/health", response_model=ApiHealth)
    async def health(session: AsyncSession = Depends(read_session)) -> ApiHealth:
        transaction_mode = str(await session.scalar(text("SHOW transaction_read_only")))
        schema_revision = str(await session.scalar(text("SELECT version_num FROM alembic_version")))
        return ApiHealth(
            service="maais-mission-control",
            status="ok",
            database_transaction=("read only" if transaction_mode == "on" else transaction_mode),
            schema_revision=schema_revision,
            checked_at=resolved_clock(),
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
        direction: str | None = None,
        disposition: str | None = None,
        reason_code: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        regime: str | None = None,
        strategy_version_id: UUID | None = None,
        gate_type: str | None = None,
        gate_passed: bool | None = None,
        agent_name: str | None = None,
        agent_direction: str | None = None,
        proposal_status: str | None = None,
        order_status: str | None = None,
        outcome: str | None = None,
        before_at: datetime | None = None,
        before_id: UUID | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: AsyncSession = Depends(read_session),
    ) -> DecisionPage:
        return await MissionControlQueryService(session).list_decisions(
            experiment_id,
            symbol=symbol,
            status=status,
            direction=direction,
            disposition=disposition,
            reason_code=reason_code,
            from_at=from_at,
            to_at=to_at,
            regime=regime,
            strategy_version_id=strategy_version_id,
            gate_type=gate_type,
            gate_passed=gate_passed,
            agent_name=agent_name,
            agent_direction=agent_direction,
            proposal_status=proposal_status,
            order_status=order_status,
            outcome=outcome,
            before_at=before_at,
            before_id=before_id,
            limit=limit,
        )

    @application.get("/api/v1/experiments/{experiment_id}/decisions/export.csv")
    async def export_decisions_csv(
        experiment_id: UUID,
        symbol: str | None = None,
        status: str | None = None,
        direction: str | None = None,
        disposition: str | None = None,
        reason_code: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        regime: str | None = None,
        strategy_version_id: UUID | None = None,
        gate_type: str | None = None,
        gate_passed: bool | None = None,
        agent_name: str | None = None,
        agent_direction: str | None = None,
        proposal_status: str | None = None,
        order_status: str | None = None,
        outcome: str | None = None,
        session: AsyncSession = Depends(read_session),
    ) -> StreamingResponse:
        filters = {
            "symbol": symbol,
            "status": status,
            "direction": direction,
            "disposition": disposition,
            "reason_code": reason_code,
            "from_at": from_at,
            "to_at": to_at,
            "regime": regime,
            "strategy_version_id": strategy_version_id,
            "gate_type": gate_type,
            "gate_passed": gate_passed,
            "agent_name": agent_name,
            "agent_direction": agent_direction,
            "proposal_status": proposal_status,
            "order_status": order_status,
            "outcome": outcome,
        }
        await MissionControlQueryService(session).list_decisions(
            experiment_id,
            **filters,
            limit=1,
        )
        factory: SessionFactory | None = application.state.session_factory
        if factory is None:
            raise RuntimeError("Mission Control session factory is unavailable")

        async def stream() -> AsyncIterator[str]:
            before_at: datetime | None = None
            before_id: UUID | None = None
            include_header = True
            async with factory() as export_session:
                async with export_session.begin():
                    await establish_read_only_snapshot(export_session)
                    queries = MissionControlQueryService(export_session)
                    while True:
                        page = await queries.list_decisions(
                            experiment_id,
                            **filters,
                            before_at=before_at,
                            before_id=before_id,
                            limit=500,
                        )
                        chunk = _decision_csv_chunk(
                            page.items,
                            include_header=include_header,
                        )
                        if chunk:
                            yield chunk
                        include_header = False
                        if not page.has_more:
                            break
                        before_at = page.next_before_at
                        before_id = page.next_before_id

        return StreamingResponse(
            stream(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="maais-decisions-{experiment_id}.csv"'
                )
            },
        )

    @application.get(
        "/api/v1/experiments/{experiment_id}/trades",
        response_model=TradePage,
    )
    async def trades(
        experiment_id: UUID,
        symbol: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        direction: str | None = None,
        regime: str | None = None,
        strategy_version_id: UUID | None = None,
        proposal_status: str | None = None,
        decision_disposition: str | None = None,
        order_status: str | None = None,
        counterfactual_status: str | None = None,
        outcome: str | None = None,
        before_at: datetime | None = None,
        before_id: UUID | None = None,
        limit: int = Query(100, ge=1, le=500),
        session: AsyncSession = Depends(read_session),
    ) -> TradePage:
        return await MissionControlQueryService(session).list_trades(
            experiment_id,
            symbol=symbol,
            from_at=from_at,
            to_at=to_at,
            direction=direction,
            regime=regime,
            strategy_version_id=strategy_version_id,
            proposal_status=proposal_status,
            decision_disposition=decision_disposition,
            order_status=order_status,
            counterfactual_status=counterfactual_status,
            outcome=outcome,
            before_at=before_at,
            before_id=before_id,
            limit=limit,
        )

    @application.get("/api/v1/experiments/{experiment_id}/trades/export.csv")
    async def export_trades_csv(
        experiment_id: UUID,
        symbol: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        direction: str | None = None,
        regime: str | None = None,
        strategy_version_id: UUID | None = None,
        proposal_status: str | None = None,
        decision_disposition: str | None = None,
        order_status: str | None = None,
        counterfactual_status: str | None = None,
        outcome: str | None = None,
        session: AsyncSession = Depends(read_session),
    ) -> StreamingResponse:
        filters = {
            "symbol": symbol,
            "from_at": from_at,
            "to_at": to_at,
            "direction": direction,
            "regime": regime,
            "strategy_version_id": strategy_version_id,
            "proposal_status": proposal_status,
            "decision_disposition": decision_disposition,
            "order_status": order_status,
            "counterfactual_status": counterfactual_status,
            "outcome": outcome,
        }
        await MissionControlQueryService(session).list_trades(
            experiment_id,
            **filters,
            limit=1,
        )
        factory: SessionFactory | None = application.state.session_factory
        if factory is None:
            raise RuntimeError("Mission Control session factory is unavailable")

        async def stream() -> AsyncIterator[str]:
            before_at: datetime | None = None
            before_id: UUID | None = None
            include_header = True
            async with factory() as export_session:
                async with export_session.begin():
                    await establish_read_only_snapshot(export_session)
                    queries = MissionControlQueryService(export_session)
                    while True:
                        page = await queries.list_trades(
                            experiment_id,
                            **filters,
                            before_at=before_at,
                            before_id=before_id,
                            limit=500,
                        )
                        chunk = _trade_csv_chunk(
                            page.items,
                            include_header=include_header,
                        )
                        if chunk:
                            yield chunk
                        include_header = False
                        if not page.has_more:
                            break
                        before_at = page.next_before_at
                        before_id = page.next_before_id

        return StreamingResponse(
            stream(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (f'attachment; filename="maais-trades-{experiment_id}.csv"')
            },
        )

    @application.get("/api/v1/decisions/{decision_id}", response_model=DecisionDetail)
    async def decision(
        decision_id: UUID,
        session: AsyncSession = Depends(read_session),
    ) -> DecisionDetail:
        return await MissionControlQueryService(session).get_decision(decision_id)

    @application.get("/api/v1/decisions/{decision_id}/export.json")
    async def export_decision_json(
        decision_id: UUID,
        session: AsyncSession = Depends(read_session),
    ) -> JSONResponse:
        detail = await MissionControlQueryService(session).get_decision(decision_id)
        return JSONResponse(
            content=detail.model_dump(mode="json"),
            headers={
                "Content-Disposition": (f'attachment; filename="maais-decision-{decision_id}.json"')
            },
        )

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
        principal: OperatorPrincipal = Depends(require_csrf),
    ) -> OperatorCommandView:
        factory: SessionFactory | None = application.state.session_factory
        if factory is None:
            factory = get_session_factory()
            application.state.session_factory = factory
        command = OperatorCommand.request(
            command_id=uuid4(),
            experiment_id=experiment_id,
            command_type=request.command_type,
            idempotency_key=request.idempotency_key,
            actor=principal.actor,
            reason=request.reason,
            payload=request.payload,
            confirmation=request.confirmation,
            requested_at=resolved_clock(),
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
                        await establish_read_only_snapshot(session)
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
                            "checked_at": resolved_clock().isoformat(),
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
