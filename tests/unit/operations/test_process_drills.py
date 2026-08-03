from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from maais.experiments.prepare import RepositoryIdentity
from maais.operations.process_drills import (
    evaluate_process_drills,
    load_verified_process_drills,
    process_drill_evidence_passes,
    write_process_drill_bundle,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest

NOW = datetime(2026, 8, 3, 2, tzinfo=timezone.utc)
MANIFEST_PATH = Path("/tmp/process-drill-manifest.json")


def _repository(manifest) -> RepositoryIdentity:
    return RepositoryIdentity(
        git_sha=manifest.git_sha,
        worktree_hash=None,
        lock_hash=manifest.lock_hash,
        schema_revision=manifest.schema_revision,
        agent_implementation_hashes={
            version.agent_name: version.implementation_hash for version in manifest.agent_versions
        },
    )


def _overview(manifest, *, checkpoint: int, epoch: int, decisions: int) -> dict[str, object]:
    return {
        "experiment": {
            "id": str(manifest.experiment_id),
            "status": "running",
            "manifest_hash": manifest.manifest_hash,
        },
        "account": {"account_version": checkpoint},
        "runtime": {
            "worker_status": "running",
            "lease_status": "active",
            "checkpoint_version": checkpoint,
            "lease_epoch": epoch,
        },
        "decisions": {"total": decisions},
        "operations": {
            "fills": checkpoint,
            "open_incidents": 0,
            "review_incidents": 0,
        },
        "incidents": [],
        "freshness": {"halted_cursors": 0, "active_recoveries": 0},
    }


def _snapshot(
    manifest,
    *,
    captured_at: str,
    worker: int,
    dashboard: int,
    scheduler: int,
    awake: int,
    checkpoint: int,
    epoch: int,
    decisions: int,
) -> dict[str, object]:
    return {
        "captured_at": captured_at,
        "state": {
            "run_purpose": "process_drill",
            "experiment_id": str(manifest.experiment_id),
            "manifest": str(MANIFEST_PATH.resolve()),
            "worker_pid": worker,
            "dashboard_pid": dashboard,
            "scheduler_pid": scheduler,
            "awake_pid": awake,
        },
        "overview": _overview(
            manifest,
            checkpoint=checkpoint,
            epoch=epoch,
            decisions=decisions,
        ),
        "ledger": {"ok": True, "error_count": 0, "errors": []},
    }


def _inputs() -> dict[str, object]:
    manifest = _live_manifest(schema_revision="0017", worktree_hash=None)
    dashboard_baseline = _snapshot(
        manifest,
        captured_at="2026-08-03T01:00:00Z",
        worker=10,
        dashboard=20,
        scheduler=30,
        awake=40,
        checkpoint=1,
        epoch=1,
        decisions=100,
    )
    dashboard_after = _snapshot(
        manifest,
        captured_at="2026-08-03T01:02:00Z",
        worker=10,
        dashboard=21,
        scheduler=30,
        awake=40,
        checkpoint=2,
        epoch=1,
        decisions=110,
    )
    worker_after = _snapshot(
        manifest,
        captured_at="2026-08-03T01:04:00Z",
        worker=11,
        dashboard=21,
        scheduler=30,
        awake=41,
        checkpoint=2,
        epoch=2,
        decisions=110,
    )
    return {
        "manifest": manifest,
        "manifest_path": MANIFEST_PATH,
        "repository": _repository(manifest),
        "dashboard_baseline": dashboard_baseline,
        "dashboard_recovery": {
            "service": "dashboard",
            "experiment_id": str(manifest.experiment_id),
            "manifest": str(MANIFEST_PATH.resolve()),
            "prior_pid": 20,
            "current_pids": {"worker": 10, "dashboard": 21, "scheduler": 30, "awake": 40},
            "before": {"overview": None, "ledger": {"ok": True}},
            "after": {"overview": dashboard_after["overview"], "ledger": {"ok": True}},
        },
        "dashboard_after": dashboard_after,
        "worker_baseline": dashboard_after,
        "worker_recovery": {
            "service": "worker",
            "experiment_id": str(manifest.experiment_id),
            "manifest": str(MANIFEST_PATH.resolve()),
            "prior_pid": 10,
            "current_pids": {"worker": 11, "dashboard": 21, "scheduler": 30, "awake": 41},
            "before": {"overview": dashboard_after["overview"], "ledger": {"ok": True}},
            "after": {"overview": worker_after["overview"], "ledger": {"ok": True}},
        },
        "worker_after": worker_after,
        "generated_at": NOW,
    }


def test_process_drills_require_exact_replacement_continuity_and_ledger_evidence() -> None:
    inputs = _inputs()
    report = evaluate_process_drills(**inputs)  # type: ignore[arg-type]

    assert report["passed"] is True
    assert report["report_id"]
    assert all(check["passed"] for check in report["checks"])  # type: ignore[union-attr]
    assert process_drill_evidence_passes(
        report,
        repository=inputs["repository"],  # type: ignore[arg-type]
        bundle_verified=True,
    )


def test_process_drills_fail_on_pid_reuse_regression_bad_ledger_or_wrong_purpose() -> None:
    inputs = _inputs()
    dashboard_recovery = dict(inputs["dashboard_recovery"])  # type: ignore[arg-type]
    dashboard_recovery["current_pids"] = {
        "worker": 10,
        "dashboard": 20,
        "scheduler": 30,
        "awake": 40,
    }
    worker_after = dict(inputs["worker_after"])  # type: ignore[arg-type]
    worker_after["ledger"] = {"ok": False, "error_count": 1}
    worker_after["overview"] = {
        **worker_after["overview"],  # type: ignore[dict-item]
        "decisions": {"total": 90},
    }
    baseline = dict(inputs["dashboard_baseline"])  # type: ignore[arg-type]
    baseline["state"] = {
        **baseline["state"],  # type: ignore[dict-item]
        "run_purpose": "soak",
    }
    inputs.update(
        dashboard_baseline=baseline,
        dashboard_recovery=dashboard_recovery,
        worker_after=worker_after,
    )

    report = evaluate_process_drills(**inputs)  # type: ignore[arg-type]
    failed = {check["name"] for check in report["checks"] if not check["passed"]}  # type: ignore[union-attr]

    assert report["passed"] is False
    assert {
        "disposable_run_purpose",
        "dashboard_process_replacement",
        "projection_monotonicity",
        "ledger_consistency",
    }.issubset(failed)


def test_process_drills_reject_operator_incidents_after_recovery() -> None:
    inputs = _inputs()
    worker_after = dict(inputs["worker_after"])  # type: ignore[arg-type]
    overview = dict(worker_after["overview"])  # type: ignore[arg-type]
    overview["operations"] = {
        **overview["operations"],  # type: ignore[dict-item]
        "open_incidents": 10,
        "review_incidents": 10,
    }
    overview["incidents"] = [
        {
            "status": "open",
            "requires_operator_review": True,
            "reason_code": "market_frame_quarantined",
        }
    ]
    worker_after["overview"] = overview
    inputs["worker_after"] = worker_after

    report = evaluate_process_drills(**inputs)  # type: ignore[arg-type]
    failed = {check["name"] for check in report["checks"] if not check["passed"]}  # type: ignore[union-attr]

    assert report["passed"] is False
    assert "incident_free_after_each_recovery" in failed


def test_process_drill_bundle_hashes_report_and_every_raw_artifact(tmp_path: Path) -> None:
    inputs = _inputs()
    report = evaluate_process_drills(**inputs)  # type: ignore[arg-type]
    sources: dict[str, Path] = {}
    for name in (
        "dashboard-baseline",
        "dashboard-recovery",
        "dashboard-after",
        "worker-baseline",
        "worker-recovery",
        "worker-after",
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(inputs[name.replace("-", "_")]), encoding="utf-8")
        sources[f"{name}.json"] = path

    paths = write_process_drill_bundle(report, sources, tmp_path / "bundles")
    loaded, verified = load_verified_process_drills(paths.directory)

    assert loaded == report
    assert verified is True
    sources_name = "dashboard-baseline.json"
    (paths.directory / sources_name).write_text("{}\n", encoding="utf-8")
    _loaded, verified = load_verified_process_drills(paths.directory)
    assert verified is False
