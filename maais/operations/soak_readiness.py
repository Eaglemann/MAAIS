"""Immutable, fail-closed verdict for the official 24-hour paper soak."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.api.queries import MissionControlQueryService
from maais.config.modes import RunMode
from maais.config.settings import Settings, get_settings
from maais.db.models.decisions import DecisionCycleModel, MarketFrameModel
from maais.db.models.operations import DataQualityEvaluationModel
from maais.db.replay import verify_ledger_consistency
from maais.domain.json import content_hash, to_json_data
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.prepare import RepositoryIdentity, capture_repository_identity
from maais.live import load_manifest_file
from maais.operations.health import evaluate_experiment_health
from maais.operations.verification import ledger_consistency_payload

UTC = timezone.utc
SOAK_DURATION = timedelta(hours=24)
SOAK_READINESS_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SoakReadinessBundlePaths:
    directory: Path
    json_path: Path
    markdown_path: Path
    manifest_path: Path


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": detail}


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, object], value)


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return parsed


def _health_state_from_overview(overview: Mapping[str, object]) -> dict[str, object]:
    state = {
        **_object(overview.get("runtime"), "overview runtime"),
        **_object(overview.get("freshness"), "overview freshness"),
        **_object(overview.get("operations"), "overview operations"),
    }
    for key in (
        "checkpoint_at",
        "lease_heartbeat_at",
        "lease_expires_at",
        "lease_released_at",
        "latest_bar_close_at",
        "latest_cursor_update_at",
    ):
        value = state.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            if value.utcoffset() != timedelta(0):
                raise ValueError(f"overview {key} must be UTC")
            continue
        state[key] = _parse_utc(value, f"overview {key}")
    return state


def _manifest_agents(manifest: ExperimentManifest) -> dict[str, str]:
    return {version.agent_name: version.implementation_hash for version in manifest.agent_versions}


def _decision_coverage(
    decision_times: Mapping[str, Sequence[datetime]],
    *,
    symbols: Sequence[str],
    started_at: datetime,
    generated_at: datetime,
    minimum_duration: timedelta,
    maximum_lag: timedelta,
) -> dict[str, object]:
    expected_symbols = set(symbols)
    observed_symbols = set(decision_times)
    rows: list[dict[str, object]] = []
    duplicate_cycles = 0
    missing_cycles = 0
    irregular_intervals = 0
    total_cycles = 0
    coverage_passed = observed_symbols == expected_symbols
    minimum_span = minimum_duration - (maximum_lag * 2)

    for symbol in sorted(expected_symbols | observed_symbols):
        values = sorted(decision_times.get(symbol, ()))
        total_cycles += len(values)
        unique = sorted(set(values))
        duplicates = len(values) - len(unique)
        duplicate_cycles += duplicates
        missing = 0
        irregular = 0
        for previous, current in zip(unique, unique[1:]):
            difference = current - previous
            seconds = difference.total_seconds()
            if seconds <= 0 or seconds % 60 != 0:
                irregular += 1
                continue
            missing += max(int(seconds // 60) - 1, 0)
        missing_cycles += missing
        irregular_intervals += irregular
        first = unique[0] if unique else None
        last = unique[-1] if unique else None
        span = last - first if first is not None and last is not None else timedelta(0)
        head_lag = first - started_at if first is not None else None
        tail_lag = generated_at - last if last is not None else None
        row_passed = bool(
            unique
            and duplicates == 0
            and missing == 0
            and irregular == 0
            and span >= minimum_span
            and head_lag is not None
            and -maximum_lag <= head_lag <= maximum_lag
            and tail_lag is not None
            and timedelta(0) <= tail_lag <= maximum_lag
        )
        coverage_passed = coverage_passed and row_passed
        rows.append(
            {
                "symbol": symbol,
                "cycles": len(values),
                "distinct_cycles": len(unique),
                "duplicate_cycles": duplicates,
                "missing_cycles": missing,
                "irregular_intervals": irregular,
                "first_cycle_at": first,
                "last_cycle_at": last,
                "span_seconds": int(span.total_seconds()),
                "head_lag_seconds": (
                    int(head_lag.total_seconds()) if head_lag is not None else None
                ),
                "tail_lag_seconds": (
                    int(tail_lag.total_seconds()) if tail_lag is not None else None
                ),
                "passed": row_passed,
            }
        )
    return {
        "passed": coverage_passed,
        "expected_symbols": len(expected_symbols),
        "observed_symbols": len(observed_symbols),
        "symbols_passed": sum(row["passed"] is True for row in rows),
        "required_span_seconds": int(minimum_span.total_seconds()),
        "total_cycles": total_cycles,
        "duplicate_cycles": duplicate_cycles,
        "missing_cycles": missing_cycles,
        "irregular_intervals": irregular_intervals,
        "symbols": rows,
    }


def evaluate_soak_readiness(
    *,
    manifest: ExperimentManifest,
    repository: RepositoryIdentity,
    settings: Settings,
    run_state: Mapping[str, object],
    preflight: Mapping[str, object],
    overview: Mapping[str, object],
    health: Mapping[str, object],
    ledger: Mapping[str, object],
    decision_times: Mapping[str, Sequence[datetime]],
    required_quality_failures: int,
    unsafe_quality_admissions: int,
    log_audit: Mapping[str, object],
    generated_at: datetime,
    minimum_duration: timedelta = SOAK_DURATION,
    maximum_lag: timedelta = timedelta(seconds=180),
) -> dict[str, object]:
    if generated_at.utcoffset() != timedelta(0):
        raise ValueError("soak verdict generated_at must be UTC")
    if minimum_duration < SOAK_DURATION:
        raise ValueError("official soak duration cannot be less than 24 hours")
    if maximum_lag <= timedelta(0):
        raise ValueError("maximum lag must be positive")
    if required_quality_failures < 0 or unsafe_quality_admissions < 0:
        raise ValueError("quality failure counts cannot be negative")

    started_at = _parse_utc(run_state.get("started_at"), "run state started_at")
    elapsed = generated_at - started_at
    experiment = _object(overview.get("experiment"), "overview experiment")
    runtime = _object(overview.get("runtime"), "overview runtime")
    decisions = _object(overview.get("decisions"), "overview decisions")
    operations = _object(overview.get("operations"), "overview operations")
    freshness = _object(overview.get("freshness"), "overview freshness")
    processes = _object(run_state.get("process_alive"), "run state process_alive")

    identity_matches = (
        repository.worktree_hash is None
        and manifest.worktree_hash is None
        and repository.git_sha == manifest.git_sha
        and repository.lock_hash == manifest.lock_hash
        and repository.schema_revision == manifest.schema_revision
        and dict(repository.agent_implementation_hashes) == _manifest_agents(manifest)
        and str(experiment.get("id")) == str(manifest.experiment_id)
        and experiment.get("git_sha") == manifest.git_sha
        and experiment.get("lock_hash") == manifest.lock_hash
        and experiment.get("schema_revision") == manifest.schema_revision
        and experiment.get("manifest_hash") == manifest.manifest_hash
        and run_state.get("experiment_id") == str(manifest.experiment_id)
    )
    safety_passed = (
        manifest.mode is RunMode.PAPER_LIVE
        and experiment.get("mode") == "paper_live"
        and not settings.binance_demo_api_key
        and not settings.binance_demo_api_secret
    )
    preflight_passed = (
        preflight.get("passed") is True
        and preflight.get("experiment_id") == str(manifest.experiment_id)
        and preflight.get("manifest_hash") == manifest.manifest_hash
    )
    process_passed = (
        processes == {"worker": True, "dashboard": True, "awake": True}
        and run_state.get("last_recovery_at") is None
    )
    operation_passed = (
        operations.get("open_incidents") == 0
        and operations.get("review_incidents") == 0
        and freshness.get("halted_cursors") == 0
        and freshness.get("active_recoveries") == 0
        and runtime.get("kill_switch_active") is False
    )
    coverage = _decision_coverage(
        decision_times,
        symbols=manifest.symbols,
        started_at=started_at,
        generated_at=generated_at,
        minimum_duration=minimum_duration,
        maximum_lag=maximum_lag,
    )
    overview_total = decisions.get("total")
    cardinality_passed = (
        coverage["passed"] is True
        and isinstance(overview_total, int)
        and not isinstance(overview_total, bool)
        and overview_total == coverage["total_cycles"]
    )
    logs_passed = (
        log_audit.get("files") == 2
        and isinstance(log_audit.get("lines"), int)
        and cast(int, log_audit["lines"]) > 0
        and log_audit.get("invalid_lines") == 0
        and log_audit.get("error_lines") == 0
    )
    checks = [
        _check(
            "paper_only_safety",
            safety_passed,
            "manifest and stored experiment are paper_live; current shell has no exchange "
            "credentials",
        ),
        _check(
            "candidate_identity",
            identity_matches,
            "repository, manifest, stored experiment, and run state share one clean identity",
        ),
        _check("preflight_evidence", preflight_passed, "frozen candidate preflight passed"),
        _check(
            "minimum_duration",
            elapsed >= minimum_duration,
            f"elapsed_seconds={int(elapsed.total_seconds())} "
            f"required={int(minimum_duration.total_seconds())}",
        ),
        _check(
            "process_continuity",
            process_passed,
            "worker, dashboard, and sleep inhibitor are alive with no official-soak restart",
        ),
        _check("runtime_health", health.get("healthy") is True, "database-backed health passed"),
        _check(
            "ledger_consistency",
            ledger.get("ok") is True,
            f"ledger errors={ledger.get('error_count', 'unknown')}",
        ),
        _check(
            "operational_state",
            operation_passed,
            "no open incidents, active recovery, halted cursor, or kill switch",
        ),
        _check(
            "decision_cardinality",
            cardinality_passed,
            f"cycles={coverage['total_cycles']} overview_total={overview_total} "
            f"symbols_passed={coverage['symbols_passed']}/{coverage['expected_symbols']} "
            f"required_span_seconds={coverage['required_span_seconds']} "
            f"missing={coverage['missing_cycles']} duplicates={coverage['duplicate_cycles']} "
            f"irregular={coverage['irregular_intervals']}",
        ),
        _check(
            "required_data_quality",
            unsafe_quality_admissions == 0,
            f"required failures={required_quality_failures} "
            f"unsafe admissions={unsafe_quality_admissions}",
        ),
        _check(
            "structured_logs",
            logs_passed,
            f"files={log_audit.get('files')} invalid={log_audit.get('invalid_lines')} "
            f"errors={log_audit.get('error_lines')}",
        ),
    ]
    passed = all(check["passed"] is True for check in checks)
    report: dict[str, object] = {
        "report_type": "soak_readiness",
        "report_schema_version": SOAK_READINESS_SCHEMA_VERSION,
        "generated_at": generated_at,
        "passed": passed,
        "verdict": "ready_for_seven_day_paper_test" if passed else "not_ready",
        "safety": {"paper_trading_only": True, "live_money": False},
        "experiment": {
            "id": str(manifest.experiment_id),
            "name": manifest.name,
            "manifest_hash": manifest.manifest_hash,
            "git_sha": manifest.git_sha,
            "lock_hash": manifest.lock_hash,
            "schema_revision": manifest.schema_revision,
        },
        "soak": {
            "started_at": started_at,
            "evaluated_at": generated_at,
            "elapsed_seconds": int(elapsed.total_seconds()),
            "required_seconds": int(minimum_duration.total_seconds()),
            "maximum_lag_seconds": int(maximum_lag.total_seconds()),
        },
        "checks": checks,
        "health": dict(health),
        "ledger": dict(ledger),
        "overview": dict(overview),
        "decision_coverage": coverage,
        "required_quality_failures": required_quality_failures,
        "unsafe_quality_admissions": unsafe_quality_admissions,
        "log_audit": dict(log_audit),
        "preflight": dict(preflight),
        "run_state": dict(run_state),
    }
    normalized = to_json_data(report)
    if not isinstance(normalized, dict):
        raise TypeError("soak readiness report must normalize to an object")
    result = cast(dict[str, object], normalized)
    result["report_id"] = content_hash(result)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_soak_readiness_markdown(report: Mapping[str, object]) -> str:
    experiment = _object(report.get("experiment"), "soak report experiment")
    soak = _object(report.get("soak"), "soak report period")
    coverage = _object(report.get("decision_coverage"), "soak report decision coverage")
    checks = report.get("checks")
    if not isinstance(checks, list):
        raise ValueError("soak report checks must be a list")
    rows: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        result = "PASS" if check.get("passed") is True else "FAIL"
        rows.append(f"| {check.get('name')} | {result} | {check.get('detail')} |")
    check_rows = "\n".join(rows)
    return f"""# MAAIS 24-hour soak verdict

> **PAPER TRADING / NO LIVE MONEY**

Verdict: **{report["verdict"]}**  
Experiment: `{experiment["id"]}`  
Candidate: `{experiment["name"]}`  
Git commit: `{experiment["git_sha"]}`  
Manifest: `{experiment["manifest_hash"]}`  
Started: `{soak["started_at"]}`  
Evaluated: `{soak["evaluated_at"]}`  
Elapsed seconds: **{soak["elapsed_seconds"]}**

| Gate | Result | Evidence |
|---|---|---|
{check_rows}

## Decision continuity

| Metric | Value |
|---|---:|
| Symbols | {coverage["observed_symbols"]} / {coverage["expected_symbols"]} |
| Decision cycles | {coverage["total_cycles"]} |
| Missing cycles | {coverage["missing_cycles"]} |
| Duplicate cycles | {coverage["duplicate_cycles"]} |
| Irregular intervals | {coverage["irregular_intervals"]} |

This verdict authorizes only the seven-day local paper test. It does not authorize live
money and does not establish strategy profitability.
"""


def write_soak_readiness_bundle(
    report: Mapping[str, object],
    output_directory: Path,
) -> SoakReadinessBundlePaths:
    report_id = report.get("report_id")
    experiment = _object(report.get("experiment"), "soak report experiment")
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise ValueError("soak report requires a SHA-256 report_id")
    experiment_id = UUID(str(experiment.get("id")))
    bundle_name = f"soak-{str(experiment_id)[:8]}-{report.get('verdict')}-{report_id[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / bundle_name
    if target.exists():
        raise FileExistsError(f"soak readiness bundle already exists: {target}")
    with tempfile.TemporaryDirectory(
        prefix=".maais-soak-readiness-", dir=output_directory
    ) as temporary:
        temporary_path = Path(temporary)
        json_path = temporary_path / "verdict.json"
        markdown_path = temporary_path / "verdict.md"
        manifest_path = temporary_path / "bundle-manifest.json"
        json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(render_soak_readiness_markdown(report), encoding="utf-8")
        artifacts = (json_path, markdown_path)
        manifest_path.write_text(
            json.dumps(
                {
                    "report_id": report_id,
                    "report_schema_version": SOAK_READINESS_SCHEMA_VERSION,
                    "artifacts": {
                        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                        for path in artifacts
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(target)
    return SoakReadinessBundlePaths(
        directory=target,
        json_path=target / "verdict.json",
        markdown_path=target / "verdict.md",
        manifest_path=target / "bundle-manifest.json",
    )


def _pid_alive(value: object) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def audit_structured_logs(paths: Sequence[Path]) -> dict[str, object]:
    errors: list[dict[str, object]] = []
    problem_count = 0
    invalid_lines = 0
    error_lines = 0
    warning_lines = 0
    line_count = 0
    existing = 0

    def record_problem(problem: dict[str, object]) -> None:
        nonlocal problem_count
        problem_count += 1
        if len(errors) < 100:
            errors.append(problem)

    for path in paths:
        if not path.is_file():
            record_problem({"path": str(path), "reason": "missing"})
            continue
        existing += 1
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            line_count += 1
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                record_problem({"path": str(path), "line": line_number, "reason": "invalid_json"})
                continue
            if not isinstance(value, dict):
                invalid_lines += 1
                record_problem({"path": str(path), "line": line_number, "reason": "not_an_object"})
                continue
            level = str(value.get("level", "")).lower()
            if level in {"error", "critical", "exception"}:
                error_lines += 1
                record_problem(
                    {
                        "path": str(path),
                        "line": line_number,
                        "level": level,
                        "event": str(value.get("event", ""))[:240],
                    }
                )
            elif level in {"warning", "warn"}:
                warning_lines += 1
    return {
        "files": existing,
        "lines": line_count,
        "invalid_lines": invalid_lines,
        "error_lines": error_lines,
        "warning_lines": warning_lines,
        "errors": errors,
        "errors_truncated": problem_count - len(errors),
    }


async def _database_soak_state(
    database_url: str,
    experiment_id: UUID,
) -> tuple[dict[str, object], dict[str, object], dict[str, tuple[datetime, ...]], int, int]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                overview_model = await MissionControlQueryService(session).get_overview(
                    experiment_id
                )
                ledger = ledger_consistency_payload(await verify_ledger_consistency(session))
                rows = (
                    await session.execute(
                        select(DecisionCycleModel.symbol, DecisionCycleModel.cycle_at)
                        .where(DecisionCycleModel.experiment_id == experiment_id)
                        .order_by(DecisionCycleModel.symbol, DecisionCycleModel.cycle_at)
                    )
                ).all()
                required_quality_failures = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(DataQualityEvaluationModel)
                        .join(
                            MarketFrameModel,
                            MarketFrameModel.id == DataQualityEvaluationModel.market_frame_id,
                        )
                        .where(
                            MarketFrameModel.experiment_id == experiment_id,
                            DataQualityEvaluationModel.required.is_(True),
                            DataQualityEvaluationModel.status == "failed",
                        )
                    )
                    or 0
                )
                unsafe_quality_admissions = int(
                    await session.scalar(
                        select(func.count(func.distinct(DecisionCycleModel.id)))
                        .select_from(DataQualityEvaluationModel)
                        .join(
                            DecisionCycleModel,
                            DecisionCycleModel.market_frame_id
                            == DataQualityEvaluationModel.market_frame_id,
                        )
                        .where(
                            DecisionCycleModel.experiment_id == experiment_id,
                            DataQualityEvaluationModel.required.is_(True),
                            DataQualityEvaluationModel.status == "failed",
                            DecisionCycleModel.status != "quarantined",
                        )
                    )
                    or 0
                )
        decision_lists: dict[str, list[datetime]] = {}
        for symbol, cycle_at in rows:
            decision_lists.setdefault(str(symbol), []).append(cycle_at)
        overview = overview_model.model_dump(mode="python")
        normalized_overview = to_json_data(overview)
        if not isinstance(normalized_overview, dict):
            raise TypeError("Mission Control overview must normalize to an object")
        return (
            cast(dict[str, object], normalized_overview),
            ledger,
            {symbol: tuple(values) for symbol, values in decision_lists.items()},
            required_quality_failures,
            unsafe_quality_admissions,
        )
    finally:
        await engine.dispose()


async def build_configured_soak_readiness(
    *,
    experiment_id: UUID,
    state_path: Path,
    repository_root: Path,
    maximum_lag: timedelta,
) -> dict[str, object]:
    settings = get_settings()
    state_value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state_value, dict):
        raise TypeError("paper run state must be a JSON object")
    run_state = cast(dict[str, object], state_value)
    manifest_path = Path(str(run_state.get("manifest", "")))
    preflight_path = Path(str(run_state.get("preflight", "")))
    manifest = load_manifest_file(manifest_path)
    preflight_value = json.loads(preflight_path.read_text(encoding="utf-8"))
    if not isinstance(preflight_value, dict):
        raise TypeError("candidate preflight must be a JSON object")
    generated_at = datetime.now(UTC)
    repository, database_state = await asyncio.gather(
        asyncio.to_thread(capture_repository_identity, repository_root),
        _database_soak_state(settings.database_url, experiment_id),
    )
    (
        overview,
        ledger,
        decision_times,
        required_quality_failures,
        unsafe_quality_admissions,
    ) = database_state
    health_state = _health_state_from_overview(overview)
    health = evaluate_experiment_health(
        state=health_state,
        ledger=ledger,
        now=generated_at,
        maximum_lag=maximum_lag,
        allow_stopped=False,
    )
    run_state = {
        **run_state,
        "state_path": str(state_path.resolve()),
        "process_alive": {
            "worker": _pid_alive(run_state.get("worker_pid")),
            "dashboard": _pid_alive(run_state.get("dashboard_pid")),
            "awake": _pid_alive(run_state.get("awake_pid")),
        },
    }
    suffix = str(experiment_id).split("-", 1)[0]
    logs = audit_structured_logs(
        (
            state_path.parent / "logs" / f"paper-worker-{suffix}.log",
            state_path.parent / "logs" / f"mission-control-{suffix}.log",
        )
    )
    return evaluate_soak_readiness(
        manifest=manifest,
        repository=repository,
        settings=settings,
        run_state=run_state,
        preflight=cast(dict[str, object], preflight_value),
        overview=overview,
        health=health,
        ledger=ledger,
        decision_times=decision_times,
        required_quality_failures=required_quality_failures,
        unsafe_quality_admissions=unsafe_quality_admissions,
        log_audit=logs,
        generated_at=generated_at,
        maximum_lag=maximum_lag,
    )
