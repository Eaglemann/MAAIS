"""Supervise automatic, contiguous Berlin-day closes for a timed paper run."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from maais.core.logging import get_logger

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

logger = get_logger(__name__)


def _parse_started_date(value: object) -> date:
    if not isinstance(value, str) or not value:
        raise ValueError("paper run state requires started_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("paper run started_at must be UTC-aware")
    return parsed.astimezone(BERLIN).date()


def next_report_date(
    state: Mapping[str, object],
    *,
    today_berlin: date,
) -> date | None:
    """Return the oldest completed, unclosed Berlin day and reject corrupt history."""
    candidate_date = _parse_started_date(state.get("started_at"))
    raw_entries = state.get("daily_reports", [])
    if not isinstance(raw_entries, list):
        raise ValueError("paper run daily_reports must be a list")

    recorded: list[date] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("paper run daily report entry must be an object")
        raw_date = raw_entry.get("report_date")
        raw_id = raw_entry.get("report_id")
        if not isinstance(raw_date, str):
            raise ValueError("paper run daily report date must be an ISO date")
        try:
            report_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError("paper run daily report date must be an ISO date") from exc
        if not isinstance(raw_id, str) or len(raw_id) != 64:
            raise ValueError("paper run daily report ID must be SHA-256")
        recorded.append(report_date)

    if recorded != sorted(set(recorded)):
        raise ValueError("paper run daily reports must be unique and ordered")
    expected = [candidate_date + timedelta(days=index) for index in range(len(recorded))]
    if recorded != expected:
        raise ValueError("paper run daily reports must be contiguous from the candidate start")
    if any(report_date >= today_berlin for report_date in recorded):
        raise ValueError("paper run daily reports may contain only completed Berlin days")

    next_date = candidate_date + timedelta(days=len(recorded))
    return next_date if next_date < today_berlin else None


def _load_state(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("paper run state must contain a JSON object")
    return cast(dict[str, object], value)


def _run_close(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def supervise_daily_closes(
    *,
    state_path: Path,
    close_script: Path,
    poll_seconds: int,
    now: Callable[[], datetime] | None = None,
    runner: CommandRunner = _run_close,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Close every completed day once; exit on ambiguity or close failure."""
    if poll_seconds <= 0:
        raise ValueError("daily supervisor poll interval must be positive")
    resolved_script = close_script.resolve(strict=True)
    observed_now = now or (lambda: datetime.now(UTC))
    logger.info(
        "daily_supervisor_started",
        state_path=str(state_path.resolve()),
        close_script=str(resolved_script),
        poll_seconds=poll_seconds,
    )
    while True:
        state = _load_state(state_path)
        experiment_id = state.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError("paper run state requires experiment_id")
        current = observed_now()
        if current.tzinfo is None:
            raise ValueError("daily supervisor clock must be timezone-aware")
        report_date = next_report_date(
            state,
            today_berlin=current.astimezone(BERLIN).date(),
        )
        if report_date is None:
            sleeper(poll_seconds)
            continue

        command = (str(resolved_script), experiment_id, report_date.isoformat())
        logger.info(
            "daily_close_started",
            experiment_id=experiment_id,
            report_date=report_date.isoformat(),
        )
        result = runner(command)
        if result.returncode != 0:
            logger.error(
                "daily_close_failed",
                experiment_id=experiment_id,
                report_date=report_date.isoformat(),
                exit_code=result.returncode,
                stdout=result.stdout[-4000:],
                stderr=result.stderr[-4000:],
            )
            raise RuntimeError(
                f"daily close failed for {report_date.isoformat()} with exit code "
                f"{result.returncode}"
            )
        logger.info(
            "daily_close_completed",
            experiment_id=experiment_id,
            report_date=report_date.isoformat(),
            stdout=result.stdout[-4000:],
        )
