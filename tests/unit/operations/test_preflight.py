from dataclasses import replace
from datetime import datetime, timezone
from typing import cast

from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.domain.json import content_hash
from maais.experiments.prepare import RepositoryIdentity
from maais.operations.preflight import evaluate_candidate_preflight
from maais.operations.qualification import (
    REQUIRED_QUALIFICATION_CHECKS,
    QualificationCheckResult,
    build_qualification_report,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest


def _repository(manifest):
    return RepositoryIdentity(
        git_sha=manifest.git_sha,
        worktree_hash=None,
        lock_hash=manifest.lock_hash,
        schema_revision=manifest.schema_revision,
        agent_implementation_hashes={
            entry.agent_name: entry.implementation_hash for entry in manifest.agent_versions
        },
    )


def _restore_verification() -> dict[str, object]:
    return {
        "passed": True,
        "source_database": "maais",
        "target_database": "maais_week_restore",
        "schema_revision": {"backup": "0015", "restored": "0015"},
        "schema_revision_match": True,
        "table_counts_match": True,
        "ledger": {"ok": True, "error_count": 0, "errors": []},
    }


def _qualification(repository: RepositoryIdentity) -> dict[str, object]:
    results = tuple(
        QualificationCheckResult(
            name=name,
            command=("verify", name),
            exit_code=0,
            duration_seconds=1,
            output_file=f"{name}.log",
            output_sha256="a" * 64,
            output_bytes=1,
        )
        for name in REQUIRED_QUALIFICATION_CHECKS
    )
    return build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=results,
        generated_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


def _soak_readiness(
    repository: RepositoryIdentity,
    *,
    generated_at: datetime,
) -> dict[str, object]:
    report: dict[str, object] = {
        "report_type": "soak_readiness",
        "report_schema_version": 2,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "passed": True,
        "verdict": "ready_for_seven_day_paper_test",
        "safety": {"paper_trading_only": True, "live_money": False},
        "repository": {
            "git_sha": repository.git_sha,
            "worktree_hash": repository.worktree_hash,
            "lock_hash": repository.lock_hash,
            "schema_revision": repository.schema_revision,
            "agent_implementation_hashes": dict(
                sorted(repository.agent_implementation_hashes.items())
            ),
        },
        "soak": {"elapsed_seconds": 86_400, "required_seconds": 86_400},
        "checks": [
            {"name": name, "passed": True, "detail": "verified"}
            for name in (
                "paper_only_safety",
                "candidate_identity",
                "postgres_cluster_identity",
                "preflight_evidence",
                "pre_soak_process_drills",
                "minimum_duration",
                "process_continuity",
                "runtime_health",
                "ledger_consistency",
                "operational_state",
                "decision_cardinality",
                "required_data_quality",
                "structured_logs",
                "daily_report_reconciliation",
            )
        ],
    }
    report["report_id"] = content_hash(report)
    return report


def test_candidate_preflight_passes_only_when_every_gate_matches() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    repository = _repository(manifest)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
        qualification=_qualification(repository),
        qualification_bundle_verified=True,
        evaluated_at=datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        run_purpose="soak",
        process_drill_evidence={"passed": True, "report_id": "b" * 64},
        process_drill_evidence_verified=True,
    )

    assert report["passed"] is True
    checks = cast(list[dict[str, object]], report["checks"])
    assert all(check["passed"] for check in checks)
    assert report["safety"] == {"paper_trading_only": True, "live_money": False}


def test_candidate_preflight_rejects_manifest_that_runtime_would_reject() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    manifest = replace(
        manifest,
        fee_policy={"maker": "0.0002", "taker": "0.0005"},
    )
    repository = _repository(manifest)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
        qualification=_qualification(repository),
        qualification_bundle_verified=True,
        evaluated_at=datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        run_purpose="soak",
        process_drill_evidence={"passed": True, "report_id": "b" * 64},
        process_drill_evidence_verified=True,
    )

    checks = cast(list[dict[str, object]], report["checks"])
    runtime_check = next(check for check in checks if check["name"] == "runtime_policy")
    assert report["passed"] is False
    assert runtime_check["passed"] is False
    assert isinstance(runtime_check["detail"], str)
    assert "venue" in runtime_check["detail"]


def test_candidate_preflight_explains_all_failed_gates() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    repository = replace(_repository(manifest), worktree_hash="f" * 64)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=Settings(
            run_mode=RunMode.REPLAY,
            binance_demo_api_key="configured",  # pragma: allowlist secret
        ),
        database_name="maais",
        database_schema_revision="0014",
        stored_manifest_hash="different",
        ledger={"ok": False, "error_count": 1, "errors": []},
        restore_verification={"passed": False},
        dashboard_built=False,
        free_disk_bytes=1,
        minimum_free_bytes=5 * 1024**3,
        qualification=_qualification(_repository(manifest)),
        qualification_bundle_verified=False,
        evaluated_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        run_purpose="soak",
        process_drill_evidence={"passed": False},
        process_drill_evidence_verified=False,
    )

    checks = cast(list[dict[str, object]], report["checks"])
    failed = {check["name"] for check in checks if not check["passed"]}
    assert report["passed"] is False
    assert {
        "repository_clean",
        "run_mode",
        "exchange_credentials_absent",
        "database_schema",
        "stored_manifest",
        "ledger_consistency",
        "restore_drill",
        "dashboard_build",
        "free_disk",
        "fresh_qualification",
        "process_drill_gate",
    }.issubset(failed)


def test_seven_day_preflight_rejects_missing_soak_readiness_evidence() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    repository = _repository(manifest)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
        qualification=_qualification(repository),
        qualification_bundle_verified=True,
        evaluated_at=datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        run_purpose="seven_day",
        process_drill_evidence={"passed": False},
        process_drill_evidence_verified=False,
        soak_readiness_evidence={"passed": False},
        soak_readiness_evidence_verified=False,
    )

    checks = cast(list[dict[str, object]], report["checks"])
    soak_check = next(check for check in checks if check["name"] == "soak_readiness_gate")
    assert report["passed"] is False
    assert soak_check["passed"] is False


def test_seven_day_preflight_rejects_stale_soak_readiness_evidence() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    repository = _repository(manifest)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
        qualification=_qualification(repository),
        qualification_bundle_verified=True,
        evaluated_at=datetime(2026, 8, 4, 1, 0, 1, tzinfo=timezone.utc),
        run_purpose="seven_day",
        process_drill_evidence={"passed": False},
        process_drill_evidence_verified=False,
        soak_readiness_evidence=_soak_readiness(
            repository,
            generated_at=datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        ),
        soak_readiness_evidence_verified=True,
    )

    checks = cast(list[dict[str, object]], report["checks"])
    soak_check = next(check for check in checks if check["name"] == "soak_readiness_gate")
    assert report["passed"] is False
    assert soak_check["passed"] is False


def test_seven_day_preflight_accepts_fresh_exact_soak_readiness_evidence() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    repository = _repository(manifest)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
        qualification=_qualification(repository),
        qualification_bundle_verified=True,
        evaluated_at=datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        run_purpose="seven_day",
        process_drill_evidence={"passed": False},
        process_drill_evidence_verified=False,
        soak_readiness_evidence=_soak_readiness(
            repository,
            generated_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        ),
        soak_readiness_evidence_verified=True,
    )

    checks = cast(list[dict[str, object]], report["checks"])
    soak_check = next(check for check in checks if check["name"] == "soak_readiness_gate")
    assert report["passed"] is True
    assert soak_check["passed"] is True
