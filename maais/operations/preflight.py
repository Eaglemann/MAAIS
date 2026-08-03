"""Fail-closed candidate gate before a timed live-data paper run."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.config.modes import RunMode
from maais.config.settings import Settings, get_settings
from maais.db.models.experiments import ExperimentModel
from maais.db.replay import verify_ledger_consistency
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.prepare import RepositoryIdentity, capture_repository_identity
from maais.experiments.runtime_policy import LivePaperPolicy, RuntimePolicyError
from maais.live import load_manifest_file
from maais.operations.qualification import (
    load_verified_qualification,
    qualification_evidence_passes,
)
from maais.operations.verification import ledger_consistency_payload

UTC = timezone.utc


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": detail}


def evaluate_candidate_preflight(
    *,
    manifest: ExperimentManifest,
    repository: RepositoryIdentity,
    settings: Settings,
    database_name: str,
    database_schema_revision: str,
    stored_manifest_hash: str | None,
    ledger: dict[str, object],
    restore_verification: dict[str, object],
    dashboard_built: bool,
    free_disk_bytes: int,
    minimum_free_bytes: int,
    qualification: dict[str, object],
    qualification_bundle_verified: bool,
    evaluated_at: datetime,
) -> dict[str, object]:
    """Evaluate every local, identity, safety, and recovery prerequisite."""
    runtime_policy_error: str | None = None
    try:
        runtime_policy = LivePaperPolicy.from_manifest(manifest)
    except RuntimePolicyError as exc:
        runtime_policy_error = str(exc)
        runtime_policy = None
    manifest_agents = {
        entry.agent_name: entry.implementation_hash for entry in manifest.agent_versions
    }
    restored_schema = restore_verification.get("schema_revision")
    restored_schema_values = restored_schema if isinstance(restored_schema, dict) else {}
    restore_passed = (
        restore_verification.get("passed") is True
        and restore_verification.get("source_database") == database_name
        and restore_verification.get("schema_revision_match") is True
        and restore_verification.get("table_counts_match") is True
        and isinstance(restore_verification.get("ledger"), dict)
        and cast(dict[str, object], restore_verification["ledger"]).get("ok") is True
        and restored_schema_values.get("restored") == manifest.schema_revision
    )
    qualification_passed = qualification_evidence_passes(
        qualification,
        repository=repository,
        bundle_verified=qualification_bundle_verified,
        evaluated_at=evaluated_at,
    )
    checks = [
        _check(
            "manifest_mode",
            manifest.mode is RunMode.PAPER_LIVE,
            f"manifest mode is {manifest.mode.value}",
        ),
        _check(
            "runtime_policy",
            runtime_policy_error is None,
            (
                "frozen live-paper runtime policy is valid; "
                f"fee_venue={runtime_policy.fee_venue} fee_tier={runtime_policy.fee_tier}"
                if runtime_policy is not None
                else f"frozen live-paper runtime policy is invalid: {runtime_policy_error}"
            ),
        ),
        _check(
            "manifest_candidate_identity",
            manifest.worktree_hash is None,
            "manifest is pinned to a clean commit"
            if manifest.worktree_hash is None
            else "manifest records a dirty worktree",
        ),
        _check(
            "repository_clean",
            repository.worktree_hash is None,
            "repository worktree is clean"
            if repository.worktree_hash is None
            else "repository worktree has uncommitted or untracked changes",
        ),
        _check(
            "repository_identity",
            repository.git_sha == manifest.git_sha
            and repository.lock_hash == manifest.lock_hash
            and repository.schema_revision == manifest.schema_revision
            and dict(repository.agent_implementation_hashes) == manifest_agents,
            "repository commit, lockfile, schema, and agent hashes match manifest",
        ),
        _check(
            "run_mode",
            settings.run_mode is RunMode.PAPER_LIVE,
            f"configured run mode is {settings.run_mode.value}",
        ),
        _check(
            "exchange_credentials_absent",
            not settings.binance_demo_api_key and not settings.binance_demo_api_secret,
            "no authenticated exchange credentials are configured",
        ),
        _check(
            "database_schema",
            database_schema_revision == manifest.schema_revision,
            f"database={database_schema_revision} manifest={manifest.schema_revision}",
        ),
        _check(
            "stored_manifest",
            stored_manifest_hash is None or stored_manifest_hash == manifest.manifest_hash,
            "experiment is new or stored manifest hash matches",
        ),
        _check(
            "ledger_consistency",
            ledger.get("ok") is True,
            f"ledger errors={ledger.get('error_count', 'unknown')}",
        ),
        _check(
            "restore_drill",
            restore_passed,
            "restore schema, table inventory, and ledger match the backup",
        ),
        _check(
            "dashboard_build",
            dashboard_built,
            "dashboard production bundle is present",
        ),
        _check(
            "free_disk",
            free_disk_bytes >= minimum_free_bytes,
            f"free_bytes={free_disk_bytes} required_bytes={minimum_free_bytes}",
        ),
        _check(
            "fresh_qualification",
            qualification_passed,
            (
                "immutable qualification bundle is verified, fresh, complete, and matches "
                "repository"
                if qualification_passed
                else "qualification bundle is missing, stale, incomplete, tampered, or does "
                "not match the exact clean repository"
            ),
        ),
    ]
    return {
        "passed": all(check["passed"] is True for check in checks),
        "experiment_id": str(manifest.experiment_id),
        "manifest_hash": manifest.manifest_hash,
        "qualification_report_id": qualification.get("report_id"),
        "safety": {"paper_trading_only": True, "live_money": False},
        "checks": checks,
    }


async def _database_preflight_state(
    database_url: str,
    experiment_id: object,
) -> tuple[str, str, str | None, dict[str, object]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                database_name = str(await session.scalar(text("SELECT current_database()")))
                schema_revision = str(
                    await session.scalar(text("SELECT version_num FROM alembic_version"))
                )
                stored_manifest_hash = await session.scalar(
                    select(ExperimentModel.manifest_hash).where(ExperimentModel.id == experiment_id)
                )
                ledger = ledger_consistency_payload(await verify_ledger_consistency(session))
        return database_name, schema_revision, stored_manifest_hash, ledger
    finally:
        await engine.dispose()


async def run_candidate_preflight(
    *,
    manifest_path: Path,
    restore_verification_path: Path,
    qualification_directory: Path,
    repository_root: Path,
    dashboard_directory: Path,
    minimum_free_gb: int,
) -> dict[str, object]:
    manifest = load_manifest_file(manifest_path)
    settings = get_settings()
    repository, database_state = await asyncio.gather(
        asyncio.to_thread(capture_repository_identity, repository_root),
        _database_preflight_state(settings.database_url, manifest.experiment_id),
    )
    restore_value = json.loads(restore_verification_path.read_text(encoding="utf-8"))
    if not isinstance(restore_value, dict):
        raise TypeError("restore verification must contain a JSON object")
    qualification, qualification_verified = load_verified_qualification(qualification_directory)
    evaluated_at = datetime.now(UTC)
    database_name, schema_revision, stored_manifest_hash, ledger = database_state
    report = evaluate_candidate_preflight(
        manifest=manifest,
        repository=repository,
        settings=settings,
        database_name=database_name,
        database_schema_revision=schema_revision,
        stored_manifest_hash=stored_manifest_hash,
        ledger=ledger,
        restore_verification=restore_value,
        dashboard_built=(dashboard_directory / "index.html").is_file(),
        free_disk_bytes=shutil.disk_usage(repository_root).free,
        minimum_free_bytes=minimum_free_gb * 1024**3,
        qualification=qualification,
        qualification_bundle_verified=qualification_verified,
        evaluated_at=evaluated_at,
    )
    report["evaluated_at"] = evaluated_at.isoformat().replace("+00:00", "Z")
    report["restore_verification_path"] = str(restore_verification_path.resolve())
    report["qualification_directory"] = str(qualification_directory.resolve())
    return report
