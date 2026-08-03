from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.experiments.prepare import RepositoryIdentity
from maais.operations.health import evaluate_experiment_health
from maais.operations.soak_readiness import (
    _health_state_from_overview,
    audit_structured_logs,
    evaluate_soak_readiness,
    write_soak_readiness_bundle,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest

NOW = datetime(2026, 8, 3, 20, 1, tzinfo=timezone.utc)


def _inputs() -> dict[str, object]:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    repository = RepositoryIdentity(
        git_sha=manifest.git_sha,
        worktree_hash=None,
        lock_hash=manifest.lock_hash,
        schema_revision=manifest.schema_revision,
        agent_implementation_hashes={
            version.agent_name: version.implementation_hash for version in manifest.agent_versions
        },
    )
    first_cycle = NOW - timedelta(hours=24)
    decision_times = {
        symbol: tuple(first_cycle + timedelta(minutes=index) for index in range(1440))
        for symbol in manifest.symbols
    }
    report_cycles = sum(
        1
        for values in decision_times.values()
        for cycle_at in values
        if cycle_at.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat() == "2026-08-02"
    )
    return {
        "manifest": manifest,
        "repository": repository,
        "settings": Settings(run_mode=RunMode.PAPER_LIVE),
        "run_state": {
            "experiment_id": str(manifest.experiment_id),
            "started_at": (NOW - timedelta(hours=24, minutes=1)).isoformat(),
            "docker_context": "desktop-linux",
            "postgres_system_identifier": "7669409277984608290",
            "process_alive": {
                "worker": True,
                "dashboard": True,
                "scheduler": True,
                "awake": True,
            },
        },
        "preflight": {
            "passed": True,
            "experiment_id": str(manifest.experiment_id),
            "manifest_hash": manifest.manifest_hash,
        },
        "overview": {
            "experiment": {
                "id": str(manifest.experiment_id),
                "mode": "paper_live",
                "git_sha": manifest.git_sha,
                "lock_hash": manifest.lock_hash,
                "schema_revision": manifest.schema_revision,
                "manifest_hash": manifest.manifest_hash,
            },
            "runtime": {"kill_switch_active": False},
            "decisions": {"total": len(manifest.symbols) * 1440},
            "operations": {"open_incidents": 0, "review_incidents": 0},
            "freshness": {"halted_cursors": 0, "active_recoveries": 0},
        },
        "health": {"healthy": True, "checks": []},
        "ledger": {"ok": True, "error_count": 0, "errors": []},
        "database_identity": {
            "database": "maais",
            "system_identifier": "7669409277984608290",
            "server_address": "172.18.0.2",
            "server_port": 5432,
        },
        "decision_times": decision_times,
        "required_quality_failures": 0,
        "unsafe_quality_admissions": 0,
        "log_audit": {
            "files": 2,
            "lines": 1000,
            "invalid_lines": 0,
            "error_lines": 0,
            "warning_lines": 1,
            "errors": [],
        },
        "daily_report_evidence": {
            "passed": True,
            "report_date": "2026-08-02",
            "experiment_id": str(manifest.experiment_id),
            "report_id": "a" * 64,
            "complete_day": True,
            "ledger_ok": True,
            "ledger_error_count": 0,
            "decision_cycles": report_cycles,
        },
        "generated_at": NOW,
        "minimum_duration": timedelta(hours=24),
        "maximum_lag": timedelta(minutes=3),
    }


def test_soak_readiness_passes_only_complete_healthy_contiguous_evidence() -> None:
    report = evaluate_soak_readiness(**_inputs())  # type: ignore[arg-type]

    assert report["passed"] is True
    assert report["verdict"] == "ready_for_seven_day_paper_test"
    assert report["safety"] == {"paper_trading_only": True, "live_money": False}
    assert report["decision_coverage"]["missing_cycles"] == 0  # type: ignore[index]
    assert all(check["passed"] for check in report["checks"])  # type: ignore[union-attr]


def test_soak_safety_uses_frozen_runtime_evidence_not_invoking_shell_mode() -> None:
    inputs = _inputs()
    inputs["settings"] = Settings(run_mode=RunMode.REPLAY)

    report = evaluate_soak_readiness(**inputs)  # type: ignore[arg-type]
    checks = {check["name"]: check for check in report["checks"]}  # type: ignore[union-attr]

    assert checks["paper_only_safety"]["passed"] is True
    assert report["passed"] is True


def test_soak_readiness_requires_verified_daily_report_reconciliation() -> None:
    inputs = _inputs()
    inputs["daily_report_evidence"] = {
        "passed": False,
        "report_date": "2026-08-02",
        "error": "no recorded complete daily report",
    }

    report = evaluate_soak_readiness(**inputs)  # type: ignore[arg-type]
    checks = {check["name"]: check for check in report["checks"]}  # type: ignore[union-attr]

    assert report["passed"] is False
    assert checks["daily_report_reconciliation"]["passed"] is False
    assert "no recorded complete daily report" in checks["daily_report_reconciliation"]["detail"]


def test_soak_readiness_rejects_a_replaced_postgresql_cluster() -> None:
    inputs = _inputs()
    inputs["database_identity"] = {
        **inputs["database_identity"],  # type: ignore[dict-item]
        "system_identifier": "7669553245924327458",
    }

    report = evaluate_soak_readiness(**inputs)  # type: ignore[arg-type]
    checks = {check["name"]: check for check in report["checks"]}  # type: ignore[union-attr]

    assert report["passed"] is False
    assert checks["postgres_cluster_identity"] == {
        "name": "postgres_cluster_identity",
        "passed": False,
        "detail": (
            "context=desktop-linux recorded=7669409277984608290 configured=7669553245924327458"
        ),
    }


def test_soak_health_restores_normalized_runtime_timestamps() -> None:
    recent = NOW - timedelta(seconds=30)
    overview = {
        "runtime": {
            "worker_status": "running",
            "checkpoint_at": recent.isoformat(),
            "lease_status": "active",
            "lease_heartbeat_at": recent.isoformat(),
            "lease_expires_at": (NOW + timedelta(seconds=30)).isoformat(),
            "kill_switch_active": False,
        },
        "freshness": {
            "expected_symbols": 1,
            "cursor_count": 1,
            "latest_bar_close_at": recent.isoformat(),
            "latest_cursor_update_at": recent.isoformat(),
            "halted_cursors": 0,
            "active_recoveries": 0,
        },
        "operations": {"open_incidents": 0, "review_incidents": 0},
    }

    state = _health_state_from_overview(overview)
    health = evaluate_experiment_health(
        state=state,
        ledger={"ok": True, "error_count": 0},
        now=NOW,
        maximum_lag=timedelta(minutes=3),
        allow_stopped=False,
    )

    assert isinstance(state["checkpoint_at"], datetime)
    assert health["healthy"] is True


def test_soak_cardinality_failure_exposes_span_and_symbol_progress() -> None:
    inputs = _inputs()
    inputs["decision_times"] = {
        symbol: values[:2]
        for symbol, values in inputs["decision_times"].items()  # type: ignore[union-attr]
    }
    inputs["overview"] = {
        **inputs["overview"],  # type: ignore[dict-item]
        "decisions": {"total": len(inputs["decision_times"]) * 2},  # type: ignore[arg-type]
    }

    report = evaluate_soak_readiness(**inputs)  # type: ignore[arg-type]
    check = next(
        item
        for item in report["checks"]
        if item["name"] == "decision_cardinality"  # type: ignore[union-attr]
    )

    assert check["passed"] is False
    assert "symbols_passed=0/" in check["detail"]
    assert "required_span_seconds=" in check["detail"]


def test_soak_cardinality_allows_bounded_cycle_before_state_file_timestamp() -> None:
    inputs = _inputs()
    decision_times = inputs["decision_times"]
    assert isinstance(decision_times, dict)
    first_cycle = next(iter(decision_times.values()))[0]
    inputs["run_state"] = {
        **inputs["run_state"],  # type: ignore[dict-item]
        "started_at": (first_cycle + timedelta(seconds=10)).isoformat(),
    }

    report = evaluate_soak_readiness(**inputs)  # type: ignore[arg-type]
    check = next(
        item
        for item in report["checks"]
        if item["name"] == "decision_cardinality"  # type: ignore[union-attr]
    )

    assert check["passed"] is True


def test_soak_readiness_explains_every_material_failure() -> None:
    inputs = _inputs()
    manifest = inputs["manifest"]
    assert hasattr(manifest, "symbols")
    decision_times = dict(inputs["decision_times"])  # type: ignore[arg-type]
    first_symbol = manifest.symbols[0]  # type: ignore[union-attr]
    decision_times[first_symbol] = (
        decision_times[first_symbol][:10] + decision_times[first_symbol][11:]
    )
    inputs["decision_times"] = decision_times
    inputs["required_quality_failures"] = 1
    inputs["unsafe_quality_admissions"] = 1
    inputs["run_state"] = {
        **inputs["run_state"],  # type: ignore[dict-item]
        "last_recovery_at": NOW.isoformat(),
    }
    inputs["log_audit"] = {
        **inputs["log_audit"],  # type: ignore[dict-item]
        "error_lines": 1,
        "errors": [{"line": 3, "level": "error"}],
    }
    inputs["repository"] = replace(inputs["repository"], worktree_hash="f" * 64)  # type: ignore[arg-type]

    report = evaluate_soak_readiness(**inputs)  # type: ignore[arg-type]
    failed = {check["name"] for check in report["checks"] if not check["passed"]}  # type: ignore[union-attr]

    assert report["passed"] is False
    assert report["verdict"] == "not_ready"
    assert {
        "candidate_identity",
        "process_continuity",
        "decision_cardinality",
        "required_data_quality",
        "structured_logs",
    }.issubset(failed)


def test_soak_readiness_bundle_is_immutable_and_hash_manifested(tmp_path: Path) -> None:
    report = evaluate_soak_readiness(**_inputs())  # type: ignore[arg-type]

    paths = write_soak_readiness_bundle(report, tmp_path)

    assert paths.json_path.is_file()
    assert paths.markdown_path.is_file()
    assert paths.manifest_path.is_file()
    assert "ready_for_seven_day_paper_test" in paths.markdown_path.read_text()
    with pytest.raises(FileExistsError, match="already exists"):
        write_soak_readiness_bundle(report, tmp_path)


def test_log_audit_counts_every_failure_but_caps_embedded_samples(tmp_path: Path) -> None:
    first = tmp_path / "worker.log"
    second = tmp_path / "dashboard.log"
    first.write_text("not-json\n" * 150, encoding="utf-8")
    second.write_text('{"level":"info","event":"started"}\n', encoding="utf-8")

    audit = audit_structured_logs((first, second))

    assert audit["invalid_lines"] == 150
    assert len(audit["errors"]) == 100  # type: ignore[arg-type]
    assert audit["errors_truncated"] == 50
