import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from maais.operations.final_reporting import (
    build_final_report_from_bundles,
    resolve_existing_daily_report_bundle,
    verify_daily_report_bundle,
    write_final_report_bundle,
)
from maais.operations.reporting import write_daily_report_bundle

EXPERIMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
BERLIN = ZoneInfo("Europe/Berlin")


def _daily_report(report_date: date, index: int) -> dict[str, object]:
    start_local = datetime.combine(report_date, time.min, BERLIN)
    end_local = start_local + timedelta(days=1)
    starting_equity = 10_000 + index
    ending_equity = starting_equity + 1
    return {
        "report_id": f"{index + 1:064x}",
        "report_type": "daily",
        "report_schema_version": 1,
        "report_date": report_date.isoformat(),
        "generated_at": (end_local.astimezone(timezone.utc) + timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z"),
        "complete_day": True,
        "safety": {
            "paper_trading_only": True,
            "live_money": False,
            "authenticated_exchange_credentials_used": False,
        },
        "experiment": {
            "id": str(EXPERIMENT_ID),
            "name": "week-candidate",
            "mode": "paper_live",
            "status": "running",
            "git_sha": "b" * 40,
            "worktree_hash": None,
            "lock_hash": "c" * 64,
            "schema_revision": "0015",
            "config_hash": "d" * 64,
            "manifest_hash": "e" * 64,
        },
        "window": {
            "timezone": "Europe/Berlin",
            "start_local": start_local.isoformat(),
            "end_local": end_local.isoformat(),
            "start_utc": start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end_utc": end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cutoff_utc": end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "account": {
            "starting_equity": str(starting_equity),
            "ending_equity": str(ending_equity),
            "net_change": "1",
            "cash_balance": str(ending_equity),
            "realized_pnl": "1",
            "ending_realized_pnl": str(index + 1),
            "unrealized_pnl": "0",
            "fees": "0.1",
            "ending_fees": str((index + 1) / 10),
            "funding": "0.01",
            "ending_funding": str((index + 1) / 100),
            "maximum_drawdown": "0.02",
            "peak_exposure": "100",
            "peak_risk_at_stop": "2",
            "peak_used_margin": "100",
        },
        "decisions": {
            "total": 10,
            "by_status": {"completed": 10},
            "by_disposition": {"approved": 1, "neutral": 9},
            "by_direction": {"long": 1, "neutral": 9},
            "by_reason": {"accepted": 1, "no_signal": 9},
            "by_symbol": {"BTCUSDT": 10},
            "by_regime": {"trending": 10},
        },
        "agents": {
            "evaluations": 80,
            "by_name": {"momentum": 10},
            "by_maturity": {"implemented": 70, "proxy": 10},
            "by_direction": {"long": 8, "neutral": 72},
            "by_reason": {"signal": 8, "no_signal": 72},
            "incompatible": 0,
            "disabled": 0,
        },
        "gates": {
            "evaluations": 5,
            "passed": 5,
            "failed": 0,
            "by_type": {"data_quality": 5},
            "failures_by_reason": {},
        },
        "data_quality": {
            "evaluations": 10,
            "by_status": {"passed": 10},
            "by_check": {"venue_clock": 10},
            "by_reason": {"venue_clock_fresh": 10},
            "failed_required": 0,
        },
        "execution": {
            "proposals": 1,
            "proposals_by_status": {"approved": 1},
            "orders_created": 1,
            "orders_by_status": {"filled": 1},
            "order_events": 3,
            "order_events_by_type": {"order.created": 1, "order.filled": 1},
            "fills": 1,
            "filled_quantity": "0.1",
            "fees": "0.1",
            "spread_cost": "0.01",
            "depth_slippage": "0.02",
            "latency_slippage": "0.03",
            "total_slippage": "0.06",
            "funding_entries": 1,
            "funding_amount": "0.01",
        },
        "counterfactuals": {
            "created": 1,
            "by_status": {"resolved": 1},
            "by_rejection_gate": {"expected_value": 1},
            "resolved_pnl": "-0.5",
        },
        "operations": {
            "incidents_detected": 0,
            "incidents_by_severity": {},
            "incidents_by_reason": {},
            "operator_review_open": 0,
            "data_quality_failed_required": 0,
            "recoveries_started": 0,
            "recoveries_by_status": {},
            "worker_restarts": 1 if index == 0 else 0,
        },
        "runtime_snapshot": {
            "worker_status": "running",
            "lease_status": "active",
            "kill_switch_active": False,
            "open_positions": 0,
            "pending_orders": 0,
            "unresolved_counterfactuals": 0,
        },
        "reconciliation": {
            "ledger_ok": True,
            "ledger_error_count": 0,
            "authoritative_record_count": 100,
            "authoritative_hash": f"{index + 20:064x}",
            "report_hash": f"{index + 40:064x}",
        },
        "decision_index": [],
        "execution_index": [],
    }


def _write_week(directory: Path, start_date: date) -> None:
    for index in range(7):
        write_daily_report_bundle(
            _daily_report(start_date + timedelta(days=index), index),
            directory,
        )


def test_daily_report_bundle_exposes_verified_soak_evidence(tmp_path: Path) -> None:
    report_date = date(2026, 8, 3)
    paths = write_daily_report_bundle(_daily_report(report_date, 0), tmp_path)

    evidence = verify_daily_report_bundle(
        paths.directory,
        expected_date=report_date,
        experiment_id=EXPERIMENT_ID,
        generated_at=datetime(2026, 8, 3, 22, 6, tzinfo=timezone.utc),
    )

    assert evidence["passed"] is True
    assert evidence["report_date"] == "2026-08-03"
    assert evidence["experiment_id"] == str(EXPERIMENT_ID)
    assert evidence["decision_cycles"] == 10
    assert evidence["ledger_ok"] is True


def test_existing_daily_report_can_be_resumed_after_state_write_crash(tmp_path: Path) -> None:
    report_date = date(2026, 8, 3)
    paths = write_daily_report_bundle(_daily_report(report_date, 0), tmp_path)

    result = resolve_existing_daily_report_bundle(
        tmp_path,
        expected_date=report_date,
        experiment_id=EXPERIMENT_ID,
        generated_at=datetime(2026, 8, 3, 22, 6, tzinfo=timezone.utc),
    )

    assert result is not None
    assert result["report_id"] == "1".zfill(64)
    assert result["directory"] == str(paths.directory)
    assert result["resumed"] is True


def test_existing_daily_report_resume_allows_a_new_output_directory(tmp_path: Path) -> None:
    result = resolve_existing_daily_report_bundle(
        tmp_path / "not-created-yet",
        expected_date=date(2026, 8, 3),
        experiment_id=EXPERIMENT_ID,
        generated_at=datetime(2026, 8, 3, 22, 6, tzinfo=timezone.utc),
    )

    assert result is None


def test_existing_daily_report_resume_refuses_ambiguous_duplicates(tmp_path: Path) -> None:
    report_date = date(2026, 8, 3)
    write_daily_report_bundle(_daily_report(report_date, 0), tmp_path)
    duplicate = _daily_report(report_date, 1)
    duplicate["report_id"] = "f" * 64
    write_daily_report_bundle(duplicate, tmp_path)

    with pytest.raises(ValueError, match="expected at most one complete daily report"):
        resolve_existing_daily_report_bundle(
            tmp_path,
            expected_date=report_date,
            experiment_id=EXPERIMENT_ID,
            generated_at=datetime(2026, 8, 3, 22, 6, tzinfo=timezone.utc),
        )


def test_final_report_verifies_and_aggregates_exactly_seven_contiguous_days(
    tmp_path: Path,
) -> None:
    start_date = date(2026, 8, 3)
    _write_week(tmp_path, start_date)

    report = build_final_report_from_bundles(
        tmp_path,
        experiment_id=EXPERIMENT_ID,
        start_date=start_date,
        days=7,
        generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
    )

    assert report["report_type"] == "final"
    assert report["period"] == {
        "timezone": "Europe/Berlin",
        "start_date": "2026-08-03",
        "end_date": "2026-08-09",
        "calendar_days": 7,
    }
    assert report["account"] == {
        "starting_equity": "10000",
        "ending_equity": "10007",
        "net_change": "7",
        "realized_pnl": "7",
        "fees": "0.7",
        "funding": "0.07",
        "maximum_drawdown": "0.02",
        "peak_exposure": "100",
        "peak_risk_at_stop": "2",
        "peak_used_margin": "100",
    }
    assert report["decisions"]["total"] == 70  # type: ignore[index]
    assert report["execution"]["fills"] == 7  # type: ignore[index]
    assert len(report["daily_reports"]) == 7  # type: ignore[arg-type]
    assert report["reconciliation"]["verified_daily_bundles"] == 7  # type: ignore[index]


def test_final_report_bundle_is_immutable_and_self_verifying(tmp_path: Path) -> None:
    start_date = date(2026, 8, 3)
    daily_directory = tmp_path / "daily"
    output_directory = tmp_path / "final"
    _write_week(daily_directory, start_date)
    report = build_final_report_from_bundles(
        daily_directory,
        experiment_id=EXPERIMENT_ID,
        start_date=start_date,
        days=7,
        generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
    )

    paths = write_final_report_bundle(report, output_directory)

    assert paths.json_path.is_file()
    assert paths.markdown_path.is_file()
    assert paths.daily_reports_csv_path.is_file()
    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    assert manifest["report_id"] == report["report_id"]
    assert set(manifest["artifacts"]) == {
        "report.json",
        "report.md",
        "daily-reports.csv",
    }
    assert "PAPER TRADING / NO LIVE MONEY" in paths.markdown_path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="final report bundle already exists"):
        write_final_report_bundle(report, output_directory)


def test_final_report_rejects_equity_discontinuity_between_daily_snapshots(
    tmp_path: Path,
) -> None:
    start_date = date(2026, 8, 3)
    for index in range(7):
        report = _daily_report(start_date + timedelta(days=index), index)
        if index == 3:
            account = report["account"]
            assert isinstance(account, dict)
            account["starting_equity"] = "9999"
            account["net_change"] = "5"
        write_daily_report_bundle(report, tmp_path)

    with pytest.raises(ValueError, match="equity discontinuity"):
        build_final_report_from_bundles(
            tmp_path,
            experiment_id=EXPERIMENT_ID,
            start_date=start_date,
            days=7,
            generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
        )


def test_final_report_rejects_a_mislabeled_berlin_day_window(tmp_path: Path) -> None:
    start_date = date(2026, 8, 3)
    for index in range(7):
        report = _daily_report(start_date + timedelta(days=index), index)
        if index == 4:
            window = report["window"]
            assert isinstance(window, dict)
            window["start_utc"] = "2026-08-06T23:00:00Z"
        write_daily_report_bundle(report, tmp_path)

    with pytest.raises(ValueError, match="Berlin window mismatch"):
        build_final_report_from_bundles(
            tmp_path,
            experiment_id=EXPERIMENT_ID,
            start_date=start_date,
            days=7,
            generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
        )


def test_final_report_rejects_daily_account_reconciliation_mismatch(tmp_path: Path) -> None:
    start_date = date(2026, 8, 3)
    for index in range(7):
        report = _daily_report(start_date + timedelta(days=index), index)
        if index == 2:
            account = report["account"]
            assert isinstance(account, dict)
            account["net_change"] = "999"
        write_daily_report_bundle(report, tmp_path)

    with pytest.raises(ValueError, match="account net change mismatch"):
        build_final_report_from_bundles(
            tmp_path,
            experiment_id=EXPERIMENT_ID,
            start_date=start_date,
            days=7,
            generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
        )


def test_final_report_rejects_missing_or_partial_daily_evidence(tmp_path: Path) -> None:
    start_date = date(2026, 8, 3)
    for index in range(7):
        report = _daily_report(start_date + timedelta(days=index), index)
        if index == 5:
            report["complete_day"] = False
        write_daily_report_bundle(report, tmp_path)

    with pytest.raises(ValueError, match="2026-08-08, found 0"):
        build_final_report_from_bundles(
            tmp_path,
            experiment_id=EXPERIMENT_ID,
            start_date=start_date,
            days=7,
            generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
        )


def test_final_report_rejects_tampered_daily_artifact(tmp_path: Path) -> None:
    start_date = date(2026, 8, 3)
    _write_week(tmp_path, start_date)
    target = next(path for path in tmp_path.iterdir() if path.name.startswith("2026-08-06-"))
    with (target / "report.md").open("a", encoding="utf-8") as handle:
        handle.write("tampered\n")

    with pytest.raises(ValueError, match="(byte size|SHA-256) mismatch"):
        build_final_report_from_bundles(
            tmp_path,
            experiment_id=EXPERIMENT_ID,
            start_date=start_date,
            days=7,
            generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
        )


def test_final_report_rejects_candidate_identity_drift(tmp_path: Path) -> None:
    start_date = date(2026, 8, 3)
    for index in range(7):
        report = _daily_report(start_date + timedelta(days=index), index)
        if index == 4:
            experiment = report["experiment"]
            assert isinstance(experiment, dict)
            experiment["manifest_hash"] = "9" * 64
        write_daily_report_bundle(report, tmp_path)

    with pytest.raises(ValueError, match="experiment identity changed"):
        build_final_report_from_bundles(
            tmp_path,
            experiment_id=EXPERIMENT_ID,
            start_date=start_date,
            days=7,
            generated_at=datetime(2026, 8, 10, 0, 5, tzinfo=timezone.utc),
        )
