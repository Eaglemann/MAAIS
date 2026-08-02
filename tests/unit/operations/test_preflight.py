from dataclasses import replace

from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.experiments.prepare import RepositoryIdentity
from maais.operations.preflight import evaluate_candidate_preflight
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


def test_candidate_preflight_passes_only_when_every_gate_matches() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=_repository(manifest),
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
    )

    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])  # type: ignore[union-attr]
    assert report["safety"] == {"paper_trading_only": True, "live_money": False}


def test_candidate_preflight_rejects_manifest_that_runtime_would_reject() -> None:
    manifest = _live_manifest(schema_revision="0015", worktree_hash=None)
    manifest = replace(
        manifest,
        fee_policy={"maker": "0.0002", "taker": "0.0005"},
    )

    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=_repository(manifest),
        settings=Settings(run_mode=RunMode.PAPER_LIVE),
        database_name="maais",
        database_schema_revision="0015",
        stored_manifest_hash=None,
        ledger={"ok": True, "error_count": 0, "errors": []},
        restore_verification=_restore_verification(),
        dashboard_built=True,
        free_disk_bytes=10 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
    )

    runtime_check = next(check for check in report["checks"] if check["name"] == "runtime_policy")
    assert report["passed"] is False
    assert runtime_check["passed"] is False
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
    )

    failed = {check["name"] for check in report["checks"] if not check["passed"]}  # type: ignore[union-attr]
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
    }.issubset(failed)
