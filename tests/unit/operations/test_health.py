from datetime import datetime, timedelta, timezone

from maais.operations.health import evaluate_experiment_health

NOW = datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc)


def _state() -> dict[str, object]:
    return {
        "worker_status": "running",
        "checkpoint_at": NOW - timedelta(seconds=5),
        "lease_status": "active",
        "lease_heartbeat_at": NOW - timedelta(seconds=5),
        "lease_expires_at": NOW + timedelta(seconds=20),
        "kill_switch_active": False,
        "expected_symbols": 10,
        "cursor_count": 10,
        "latest_bar_close_at": NOW - timedelta(seconds=60),
        "latest_cursor_update_at": NOW - timedelta(seconds=55),
        "halted_cursors": 0,
        "active_recoveries": 0,
        "open_incidents": 0,
        "review_incidents": 0,
    }


def test_experiment_health_requires_runtime_data_and_ledger_freshness() -> None:
    report = evaluate_experiment_health(
        state=_state(),
        ledger={"ok": True, "error_count": 0, "errors": []},
        now=NOW,
        maximum_lag=timedelta(seconds=180),
        allow_stopped=False,
    )

    assert report["healthy"] is True
    assert report["status"] == "healthy"
    assert all(check["passed"] for check in report["checks"])  # type: ignore[union-attr]


def test_experiment_health_reports_every_critical_condition() -> None:
    state = _state()
    state.update(
        {
            "worker_status": "halted",
            "checkpoint_at": NOW - timedelta(minutes=10),
            "lease_status": "released",
            "lease_heartbeat_at": NOW - timedelta(minutes=10),
            "lease_expires_at": NOW - timedelta(minutes=9),
            "kill_switch_active": True,
            "cursor_count": 9,
            "latest_cursor_update_at": NOW - timedelta(minutes=10),
            "halted_cursors": 1,
            "active_recoveries": 1,
            "open_incidents": 2,
            "review_incidents": 1,
        }
    )

    report = evaluate_experiment_health(
        state=state,
        ledger={"ok": False, "error_count": 1, "errors": []},
        now=NOW,
        maximum_lag=timedelta(seconds=180),
        allow_stopped=False,
    )

    failed = {check["name"] for check in report["checks"] if not check["passed"]}  # type: ignore[union-attr]
    assert report["healthy"] is False
    assert report["status"] == "critical"
    assert {
        "ledger_consistency",
        "worker_state",
        "checkpoint_freshness",
        "active_lease",
        "cursor_coverage",
        "cursor_freshness",
        "halted_cursors",
        "active_recoveries",
        "operator_review_incidents",
        "kill_switch",
    }.issubset(failed)
