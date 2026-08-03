from datetime import date, timedelta, timezone
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from maais.operations.reporting import (
    berlin_daily_window,
    render_daily_report_markdown,
    write_daily_report_bundle,
)


def _report() -> dict[str, object]:
    return {
        "report_id": "a" * 64,
        "report_type": "daily",
        "report_schema_version": 2,
        "report_date": "2026-08-02",
        "generated_at": "2026-08-03T00:05:00Z",
        "experiment": {
            "id": "11111111-1111-4111-8111-111111111111",
            "name": "week-candidate",
            "mode": "paper_live",
            "status": "running",
            "git_sha": "b" * 40,
            "manifest_hash": "c" * 64,
            "schema_revision": "0015",
        },
        "window": {
            "timezone": "Europe/Berlin",
            "start_local": "2026-08-02T00:00:00+02:00",
            "end_local": "2026-08-03T00:00:00+02:00",
            "start_utc": "2026-08-01T22:00:00Z",
            "end_utc": "2026-08-02T22:00:00Z",
        },
        "account": {
            "starting_equity": "10000",
            "ending_equity": "10012.5",
            "net_change": "12.5",
            "realized_pnl": "10",
            "unrealized_pnl": "2.5",
            "fees": "1",
            "funding": "0",
            "maximum_drawdown": "0.003",
        },
        "decisions": {
            "total": 100,
            "by_status": {"completed": 90, "quarantined": 10},
            "by_disposition": {"approved": 4, "neutral": 86, "rejected": 10},
            "by_reason": {"accepted": 4, "insufficient_history": 10},
            "by_symbol": {"BTCUSDT": 50, "ETHUSDT": 50},
        },
        "decision_index": [
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "cycle_at": "2026-08-02T12:00:00Z",
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "regime": "trending",
                "status": "completed",
                "direction": "long",
                "disposition": "approved",
                "reason_code": "accepted",
                "market_frame_id": "33333333-3333-4333-8333-333333333333",
                "content_hash": "f" * 64,
            }
        ],
        "execution": {
            "proposals": 4,
            "orders_created": 4,
            "fills": 3,
            "filled_quantity": "0.1",
            "fees": "1",
            "spread_cost": "0.2",
            "depth_slippage": "0.1",
            "latency_slippage": "0.05",
            "total_slippage": "0.35",
        },
        "execution_index": [],
        "operations": {
            "incidents_detected": 0,
            "operator_review_open": 0,
            "data_quality_failed_required": 0,
            "recoveries_started": 0,
            "worker_restarts": 0,
        },
        "runtime_snapshot": {
            "worker_status": "running",
            "lease_status": "active",
            "kill_switch_active": False,
            "open_positions": 1,
            "pending_orders": 0,
            "unresolved_counterfactuals": 0,
        },
        "operator_actions": {
            "events": 3,
            "requests": 1,
            "rejections": 0,
            "recoveries": 0,
            "by_event_type": {
                "operator_command.requested": 1,
                "operator_command.accepted": 1,
                "operator_command.completed": 1,
            },
            "by_command_type": {"pause": 3},
            "by_status": {"requested": 1, "accepted": 1, "completed": 1},
        },
        "operator_action_index": [
            {
                "global_position": 10,
                "command_id": "44444444-4444-4444-8444-444444444444",
                "event_type": "operator_command.completed",
                "event_at": "2026-08-02T12:01:02Z",
                "command_type": "pause",
                "status": "completed",
                "actor": "local_operator",
                "reason": "inspect unexpected signal concentration",
                "payload": {"source": "mission_control"},
                "operator_confirmed": True,
                "request_hash": "9" * 64,
                "accepted_by": "paper_worker:test",
                "result": {"experiment_status": "paused"},
                "version": 3,
            }
        ],
        "reconciliation": {
            "ledger_ok": True,
            "ledger_error_count": 0,
            "authoritative_record_count": 100,
            "authoritative_hash": "d" * 64,
            "report_hash": "e" * 64,
        },
    }


def test_berlin_daily_window_handles_dst_boundaries() -> None:
    spring = berlin_daily_window(date(2026, 3, 29))
    autumn = berlin_daily_window(date(2026, 10, 25))

    assert spring.end_utc - spring.start_utc == timedelta(hours=23)
    assert autumn.end_utc - autumn.start_utc == timedelta(hours=25)
    assert spring.start_utc.tzinfo is timezone.utc


def test_daily_report_markdown_surfaces_safety_and_reconciliation() -> None:
    rendered = render_daily_report_markdown(_report())

    assert "PAPER TRADING / NO LIVE MONEY" in rendered
    assert "Ledger consistency | PASS" in rendered
    assert "insufficient_history" in rendered
    assert "10012.5" in rendered
    assert "Operator action trail" in rendered
    assert "inspect unexpected signal concentration" in rendered
    assert "paper_worker:test" in rendered


def test_report_bundle_is_immutable_and_contains_json_and_markdown(tmp_path: Path) -> None:
    paths = write_daily_report_bundle(_report(), tmp_path)

    assert paths.json_path.is_file()
    assert paths.markdown_path.is_file()
    assert paths.decisions_csv_path.is_file()
    assert paths.decisions_parquet_path.is_file()
    assert paths.execution_csv_path.is_file()
    assert paths.execution_parquet_path.is_file()
    assert paths.manifest_path.is_file()
    assert paths.directory.name.startswith("2026-08-02-11111111-")
    with duckdb.connect() as connection:
        decision_summary = connection.execute(
            "SELECT count(*), min(cycle_at) FROM read_parquet(?)",
            [str(paths.decisions_parquet_path)],
        ).fetchone()
        execution_count = connection.execute(
            "SELECT count(*) FROM read_parquet(?)",
            [str(paths.execution_parquet_path)],
        ).fetchone()
    assert decision_summary is not None
    assert decision_summary[0] == 1
    assert str(decision_summary[1]) == "2026-08-02 12:00:00"
    assert execution_count == (0,)
    with pytest.raises(FileExistsError, match="report bundle already exists"):
        write_daily_report_bundle(_report(), tmp_path)


def test_report_bundle_rejects_mismatched_experiment_identity(tmp_path: Path) -> None:
    report = _report()
    experiment = dict(report["experiment"])  # type: ignore[arg-type]
    experiment["id"] = str(UUID(int=0))
    report["experiment"] = experiment

    with pytest.raises(ValueError, match="non-zero experiment UUID"):
        write_daily_report_bundle(report, tmp_path)
