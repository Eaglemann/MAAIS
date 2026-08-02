"""Database-backed health gate for a running paper experiment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.api.queries import MissionControlQueryService
from maais.config.settings import get_settings
from maais.db.replay import verify_ledger_consistency
from maais.domain.json import to_json_data
from maais.monitoring.alerting import AlertDispatcher
from maais.operations.verification import ledger_consistency_payload

UTC = timezone.utc


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": detail}


def _fresh(
    value: object,
    *,
    now: datetime,
    maximum_lag: timedelta,
) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    age = now - value.astimezone(UTC)
    return timedelta(0) <= age <= maximum_lag


def _integer(state: dict[str, object], key: str) -> int:
    value = state.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return value


def evaluate_experiment_health(
    *,
    state: dict[str, object],
    ledger: dict[str, object],
    now: datetime,
    maximum_lag: timedelta,
    allow_stopped: bool,
) -> dict[str, object]:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("health evaluation time must be UTC-aware")
    if maximum_lag <= timedelta(0):
        raise ValueError("maximum lag must be positive")
    worker_status = state.get("worker_status")
    running = worker_status == "running"
    stopped_allowed = allow_stopped and worker_status == "stopped"
    worker_passed = running or stopped_allowed
    checkpoint_passed = (
        _fresh(state.get("checkpoint_at"), now=now, maximum_lag=maximum_lag)
        if running
        else stopped_allowed
    )
    lease_passed = (
        state.get("lease_status") == "active"
        and _fresh(state.get("lease_heartbeat_at"), now=now, maximum_lag=maximum_lag)
        and isinstance(state.get("lease_expires_at"), datetime)
        and cast(datetime, state["lease_expires_at"]).astimezone(UTC) > now
        if running
        else stopped_allowed and state.get("lease_status") == "released"
    )
    expected_symbols = _integer(state, "expected_symbols")
    cursor_count = _integer(state, "cursor_count")
    cursor_fresh = (
        _fresh(state.get("latest_cursor_update_at"), now=now, maximum_lag=maximum_lag)
        and _fresh(state.get("latest_bar_close_at"), now=now, maximum_lag=maximum_lag)
        if running
        else stopped_allowed
    )
    checks = [
        _check(
            "ledger_consistency",
            ledger.get("ok") is True,
            f"ledger errors={ledger.get('error_count', 'unknown')}",
        ),
        _check("worker_state", worker_passed, f"worker status={worker_status}"),
        _check(
            "checkpoint_freshness",
            checkpoint_passed,
            f"checkpoint_at={state.get('checkpoint_at')}",
        ),
        _check(
            "active_lease",
            bool(lease_passed),
            f"lease status={state.get('lease_status')} heartbeat={state.get('lease_heartbeat_at')}",
        ),
        _check(
            "cursor_coverage",
            expected_symbols > 0 and cursor_count == expected_symbols,
            f"cursor_count={cursor_count} expected_symbols={expected_symbols}",
        ),
        _check(
            "cursor_freshness",
            cursor_fresh,
            f"latest_bar={state.get('latest_bar_close_at')} "
            f"latest_update={state.get('latest_cursor_update_at')}",
        ),
        _check(
            "halted_cursors",
            _integer(state, "halted_cursors") == 0,
            f"halted={state.get('halted_cursors')}",
        ),
        _check(
            "active_recoveries",
            _integer(state, "active_recoveries") == 0,
            f"active={state.get('active_recoveries')}",
        ),
        _check(
            "open_incidents",
            _integer(state, "open_incidents") == 0,
            f"open={state.get('open_incidents')}",
        ),
        _check(
            "operator_review_incidents",
            _integer(state, "review_incidents") == 0,
            f"review={state.get('review_incidents')}",
        ),
        _check(
            "kill_switch",
            state.get("kill_switch_active") is False,
            f"active={state.get('kill_switch_active')}",
        ),
    ]
    healthy = all(check["passed"] is True for check in checks)
    return {
        "healthy": healthy,
        "status": "healthy" if healthy else "critical",
        "checked_at": now,
        "maximum_lag_seconds": int(maximum_lag.total_seconds()),
        "allow_stopped": allow_stopped,
        "checks": checks,
    }


async def collect_configured_experiment_health(
    experiment_id: UUID,
    *,
    maximum_lag: timedelta,
    allow_stopped: bool,
    send_alert: bool,
) -> dict[str, object]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                overview = await MissionControlQueryService(session).get_overview(experiment_id)
                ledger = ledger_consistency_payload(await verify_ledger_consistency(session))
        state = {
            **overview.runtime.model_dump(),
            **overview.freshness.model_dump(),
            **overview.operations.model_dump(),
        }
        report = evaluate_experiment_health(
            state=state,
            ledger=ledger,
            now=datetime.now(UTC),
            maximum_lag=maximum_lag,
            allow_stopped=allow_stopped,
        )
        report["experiment_id"] = str(experiment_id)
        report["experiment_status"] = overview.experiment.status
        report["account"] = overview.account.model_dump()
        normalized = to_json_data(report)
        if not isinstance(normalized, dict):
            raise TypeError("health report must be a JSON object")
        result = cast(dict[str, object], normalized)
        if send_alert and result["healthy"] is not True:
            failed = [
                str(check["name"])
                for check in cast(list[dict[str, object]], result["checks"])
                if check["passed"] is not True
            ]
            await AlertDispatcher(
                settings.telegram_bot_token or None,
                settings.telegram_chat_id or None,
            ).send_critical(
                "paper_health",
                "Paper experiment health check failed",
                ", ".join(failed),
                experiment_id=str(experiment_id),
            )
        return result
    finally:
        await engine.dispose()
