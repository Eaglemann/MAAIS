from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from maais.operations.daily_supervisor import next_report_date, supervise_daily_closes


def _state(*, started_at: str = "2026-08-03T22:00:00Z") -> dict[str, object]:
    return {
        "experiment_id": "11111111-1111-4111-8111-111111111111",
        "started_at": started_at,
    }


def test_next_report_date_starts_with_the_candidate_berlin_date() -> None:
    assert next_report_date(_state(), today_berlin=date(2026, 8, 5)) == date(2026, 8, 4)


def test_next_report_date_advances_contiguously_after_recorded_closes() -> None:
    state = {
        **_state(),
        "daily_reports": [
            {"report_date": "2026-08-04", "report_id": "a" * 64},
            {"report_date": "2026-08-05", "report_id": "b" * 64},
        ],
    }

    assert next_report_date(state, today_berlin=date(2026, 8, 7)) == date(2026, 8, 6)


def test_next_report_date_waits_while_the_next_berlin_day_is_in_progress() -> None:
    assert next_report_date(_state(), today_berlin=date(2026, 8, 4)) is None


@pytest.mark.parametrize(
    "daily_reports",
    [
        [{"report_date": "2026-08-05", "report_id": "a" * 64}],
        [
            {"report_date": "2026-08-04", "report_id": "a" * 64},
            {"report_date": "2026-08-04", "report_id": "b" * 64},
        ],
        [{"report_date": "not-a-date", "report_id": "a" * 64}],
    ],
)
def test_next_report_date_rejects_gaps_duplicates_and_invalid_dates(
    daily_reports: list[dict[str, str]],
) -> None:
    with pytest.raises(ValueError):
        next_report_date(
            {**_state(), "daily_reports": daily_reports},
            today_berlin=date(2026, 8, 7),
        )


def test_supervisor_closes_one_due_day_then_waits(tmp_path: Path) -> None:
    state_path = tmp_path / "current.json"
    close_script = tmp_path / "daily-paper-ops.sh"
    close_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    state_path.write_text(json.dumps(_state()), encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def close(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        state_path.write_text(
            json.dumps(
                {
                    **_state(),
                    "daily_reports": [{"report_date": "2026-08-04", "report_id": "a" * 64}],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="closed", stderr="")

    def stop_after_wait(_seconds: float) -> None:
        raise StopIteration

    with pytest.raises(StopIteration):
        supervise_daily_closes(
            state_path=state_path,
            close_script=close_script,
            poll_seconds=30,
            now=lambda: datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            runner=close,
            sleeper=stop_after_wait,
        )

    assert commands == [
        (
            str(close_script.resolve()),
            "11111111-1111-4111-8111-111111111111",
            "2026-08-04",
        )
    ]


def test_supervisor_exits_when_a_daily_close_fails(tmp_path: Path) -> None:
    state_path = tmp_path / "current.json"
    close_script = tmp_path / "daily-paper-ops.sh"
    close_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    state_path.write_text(json.dumps(_state()), encoding="utf-8")

    def fail(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 17, stdout="", stderr="backup failed")

    with pytest.raises(RuntimeError, match="exit code 17"):
        supervise_daily_closes(
            state_path=state_path,
            close_script=close_script,
            poll_seconds=30,
            now=lambda: datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
            runner=fail,
            sleeper=lambda _seconds: None,
        )
