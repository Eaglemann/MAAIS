"""Immutable evidence for disposable dashboard and worker recovery drills."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from maais.domain.json import content_hash, to_json_data
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.prepare import RepositoryIdentity, capture_repository_identity
from maais.live import load_manifest_file

PROCESS_DRILL_SCHEMA_VERSION = 2
PROCESS_DRILL_CHECKS = (
    "candidate_identity",
    "disposable_run_purpose",
    "experiment_identity",
    "timeline",
    "dashboard_process_replacement",
    "dashboard_worker_continuity",
    "dashboard_checkpoint_progress",
    "worker_process_replacement",
    "worker_lease_takeover",
    "worker_other_process_continuity",
    "projection_monotonicity",
    "ledger_consistency",
    "healthy_after_each_recovery",
    "incident_free_after_each_recovery",
)
PROCESS_DRILL_ARTIFACTS = (
    "dashboard-baseline.json",
    "dashboard-recovery.json",
    "dashboard-after.json",
    "worker-baseline.json",
    "worker-recovery.json",
    "worker-after.json",
)


@dataclass(frozen=True, slots=True)
class ProcessDrillBundlePaths:
    directory: Path
    report_path: Path
    manifest_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_payload(repository: RepositoryIdentity) -> dict[str, object]:
    return {
        "git_sha": repository.git_sha,
        "worktree_hash": repository.worktree_hash,
        "lock_hash": repository.lock_hash,
        "schema_revision": repository.schema_revision,
        "agent_implementation_hashes": dict(sorted(repository.agent_implementation_hashes.items())),
    }


def _object(container: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, object], value)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() == timedelta(0) else None


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": detail}


def _pid(state: Mapping[str, object], name: str) -> int | None:
    value = state.get(f"{name}_pid")
    return value if isinstance(value, int) and value > 0 else None


def _current_pid(recovery: Mapping[str, object], name: str) -> int | None:
    value = _object(recovery, "current_pids").get(name)
    return value if isinstance(value, int) and value > 0 else None


def _overview(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    return _object(snapshot, "overview")


def _counter(snapshot: Mapping[str, object], section: str, key: str) -> int | None:
    value = _object(_overview(snapshot), section).get(key)
    return value if isinstance(value, int) and value >= 0 else None


def _ledger_ok(value: Mapping[str, object]) -> bool:
    return value.get("ok") is True


def _snapshot_healthy(snapshot: Mapping[str, object]) -> bool:
    overview = _overview(snapshot)
    runtime = _object(overview, "runtime")
    freshness = _object(overview, "freshness")
    experiment = _object(overview, "experiment")
    return (
        experiment.get("status") == "running"
        and runtime.get("worker_status") == "running"
        and runtime.get("lease_status") == "active"
        and freshness.get("halted_cursors") == 0
        and freshness.get("active_recoveries") == 0
    )


def _overview_incident_free(overview: Mapping[str, object]) -> bool:
    operations = _object(overview, "operations")
    incidents = overview.get("incidents")
    return (
        operations.get("open_incidents") == 0
        and operations.get("review_incidents") == 0
        and isinstance(incidents, list)
        and not any(
            isinstance(incident, Mapping)
            and (
                incident.get("status") == "open" or incident.get("requires_operator_review") is True
            )
            for incident in incidents
        )
    )


def _monotonic(values: Sequence[int | None]) -> bool:
    return all(value is not None for value in values) and all(
        cast(int, current) >= cast(int, previous) for previous, current in zip(values, values[1:])
    )


def evaluate_process_drills(
    *,
    manifest: ExperimentManifest,
    manifest_path: Path,
    repository: RepositoryIdentity,
    dashboard_baseline: Mapping[str, object],
    dashboard_recovery: Mapping[str, object],
    dashboard_after: Mapping[str, object],
    worker_baseline: Mapping[str, object],
    worker_recovery: Mapping[str, object],
    worker_after: Mapping[str, object],
    generated_at: datetime,
) -> dict[str, object]:
    """Evaluate process replacement without accepting screenshots or manual claims."""
    if generated_at.utcoffset() != timedelta(0):
        raise ValueError("process drill generated_at must be UTC")
    snapshots = (
        dashboard_baseline,
        dashboard_after,
        worker_baseline,
        worker_after,
    )
    states = tuple(_object(snapshot, "state") for snapshot in snapshots)
    expected_manifest = str(manifest_path.resolve())
    expected_experiment = str(manifest.experiment_id)
    manifest_agents = {
        version.agent_name: version.implementation_hash for version in manifest.agent_versions
    }
    candidate_identity = (
        repository.worktree_hash is None
        and manifest.worktree_hash is None
        and repository.git_sha == manifest.git_sha
        and repository.lock_hash == manifest.lock_hash
        and repository.schema_revision == manifest.schema_revision
        and dict(repository.agent_implementation_hashes) == manifest_agents
    )
    purpose_ok = all(state.get("run_purpose") == "process_drill" for state in states)
    experiment_ok = all(
        state.get("experiment_id") == expected_experiment
        and state.get("manifest") == expected_manifest
        and _object(_overview(snapshot), "experiment").get("id") == expected_experiment
        and _object(_overview(snapshot), "experiment").get("manifest_hash")
        == manifest.manifest_hash
        for state, snapshot in zip(states, snapshots)
    ) and all(
        recovery.get("experiment_id") == expected_experiment
        and recovery.get("manifest") == expected_manifest
        for recovery in (dashboard_recovery, worker_recovery)
    )
    captured = tuple(_parse_utc(snapshot.get("captured_at")) for snapshot in snapshots)
    timeline_ok = (
        all(value is not None for value in captured)
        and all(
            cast(datetime, current) >= cast(datetime, previous)
            for previous, current in zip(captured, captured[1:])
        )
        and cast(datetime, captured[-1]) <= generated_at
    )

    dashboard_state = states[0]
    dashboard_after_state = states[1]
    dashboard_replaced = (
        dashboard_recovery.get("service") == "dashboard"
        and dashboard_recovery.get("prior_pid") == _pid(dashboard_state, "dashboard")
        and _current_pid(dashboard_recovery, "dashboard")
        == _pid(dashboard_after_state, "dashboard")
        and _pid(dashboard_after_state, "dashboard") != _pid(dashboard_state, "dashboard")
    )
    dashboard_worker_continuity = (
        _pid(dashboard_state, "worker")
        == _pid(dashboard_after_state, "worker")
        == _current_pid(dashboard_recovery, "worker")
        and _pid(dashboard_state, "scheduler")
        == _pid(dashboard_after_state, "scheduler")
        == _current_pid(dashboard_recovery, "scheduler")
        and _pid(dashboard_state, "awake")
        == _pid(dashboard_after_state, "awake")
        == _current_pid(dashboard_recovery, "awake")
    )
    dashboard_checkpoint_progress = (
        _counter(dashboard_after, "runtime", "checkpoint_version") is not None
        and _counter(dashboard_baseline, "runtime", "checkpoint_version") is not None
        and cast(int, _counter(dashboard_after, "runtime", "checkpoint_version"))
        > cast(int, _counter(dashboard_baseline, "runtime", "checkpoint_version"))
    )

    worker_state = states[2]
    worker_after_state = states[3]
    worker_replaced = (
        worker_recovery.get("service") == "worker"
        and worker_recovery.get("prior_pid") == _pid(worker_state, "worker")
        and _current_pid(worker_recovery, "worker") == _pid(worker_after_state, "worker")
        and _pid(worker_after_state, "worker") != _pid(worker_state, "worker")
    )
    before_epoch = _counter(worker_baseline, "runtime", "lease_epoch")
    after_epoch = _counter(worker_after, "runtime", "lease_epoch")
    worker_lease_takeover = (
        before_epoch is not None and after_epoch is not None and after_epoch > before_epoch
    )
    worker_other_continuity = (
        _pid(worker_state, "dashboard")
        == _pid(worker_after_state, "dashboard")
        == _current_pid(worker_recovery, "dashboard")
        and _pid(worker_state, "scheduler")
        == _pid(worker_after_state, "scheduler")
        == _current_pid(worker_recovery, "scheduler")
        and _pid(worker_after_state, "awake") == _current_pid(worker_recovery, "awake")
        and _pid(worker_after_state, "awake") != _pid(worker_state, "awake")
    )

    counter_specs = (
        ("account", "account_version"),
        ("runtime", "checkpoint_version"),
        ("decisions", "total"),
        ("operations", "fills"),
    )
    monotonicity = all(
        _monotonic(tuple(_counter(snapshot, section, key) for snapshot in snapshots))
        for section, key in counter_specs
    )
    ledger_values = [_object(snapshot, "ledger") for snapshot in snapshots] + [
        _object(_object(recovery, phase), "ledger")
        for recovery in (dashboard_recovery, worker_recovery)
        for phase in ("before", "after")
    ]
    ledger_ok = all(_ledger_ok(value) for value in ledger_values)
    healthy = _snapshot_healthy(dashboard_after) and _snapshot_healthy(worker_after)
    recovery_overviews = (
        _overview(dashboard_after),
        _overview(worker_after),
        _object(_object(dashboard_recovery, "after"), "overview"),
        _object(_object(worker_recovery, "after"), "overview"),
    )
    incident_free = all(_overview_incident_free(overview) for overview in recovery_overviews)

    checks = [
        _check("candidate_identity", candidate_identity, "manifest matches the exact clean commit"),
        _check(
            "disposable_run_purpose",
            purpose_ok,
            "every snapshot is explicitly marked process_drill",
        ),
        _check(
            "experiment_identity",
            experiment_ok,
            "snapshots and recovery records match one manifest and experiment",
        ),
        _check("timeline", timeline_ok, "snapshot chronology is valid and complete"),
        _check(
            "dashboard_process_replacement",
            dashboard_replaced,
            "dashboard PID changed exactly once through audited recovery",
        ),
        _check(
            "dashboard_worker_continuity",
            dashboard_worker_continuity,
            "worker, scheduler, and sleep inhibitor survived the dashboard fault",
        ),
        _check(
            "dashboard_checkpoint_progress",
            dashboard_checkpoint_progress,
            "worker checkpoint advanced while Mission Control was unavailable",
        ),
        _check(
            "worker_process_replacement",
            worker_replaced,
            "worker PID changed exactly once through audited recovery",
        ),
        _check(
            "worker_lease_takeover",
            worker_lease_takeover,
            "replacement worker acquired a strictly higher lease epoch",
        ),
        _check(
            "worker_other_process_continuity",
            worker_other_continuity,
            "dashboard and scheduler survived; sleep inhibitor followed the worker",
        ),
        _check(
            "projection_monotonicity",
            monotonicity,
            "account, checkpoint, decision, and fill counters never regressed",
        ),
        _check(
            "ledger_consistency",
            ledger_ok,
            "every before, recovery, and after ledger verification passed",
        ),
        _check(
            "healthy_after_each_recovery",
            healthy,
            "runtime lease and cursors are healthy after both recoveries",
        ),
        _check(
            "incident_free_after_each_recovery",
            incident_free,
            "no open or operator-review incidents remain after either recovery",
        ),
    ]
    if tuple(check["name"] for check in checks) != PROCESS_DRILL_CHECKS:
        raise RuntimeError("process drill checks differ from the required contract")
    base = {
        "process_drill_schema_version": PROCESS_DRILL_SCHEMA_VERSION,
        "generated_at": generated_at,
        "passed": all(check["passed"] is True for check in checks),
        "experiment_id": expected_experiment,
        "manifest_hash": manifest.manifest_hash,
        "repository": _repository_payload(repository),
        "required_checks": list(PROCESS_DRILL_CHECKS),
        "checks": checks,
    }
    normalized = to_json_data(base)
    if not isinstance(normalized, dict):
        raise TypeError("process drill report must normalize to an object")
    report = cast(dict[str, object], normalized)
    report["report_id"] = content_hash(report)
    return report


def process_drill_evidence_passes(
    report: Mapping[str, object],
    *,
    repository: RepositoryIdentity,
    bundle_verified: bool,
) -> bool:
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        return False
    names = [cast(Mapping[str, object], check).get("name") for check in checks]
    without_id = {key: value for key, value in report.items() if key != "report_id"}
    return (
        bundle_verified
        and report.get("process_drill_schema_version") == PROCESS_DRILL_SCHEMA_VERSION
        and report.get("passed") is True
        and report.get("repository") == _repository_payload(repository)
        and report.get("required_checks") == list(PROCESS_DRILL_CHECKS)
        and names == list(PROCESS_DRILL_CHECKS)
        and all(cast(Mapping[str, object], check).get("passed") is True for check in checks)
        and report.get("report_id") == content_hash(without_id)
    )


def write_process_drill_bundle(
    report: Mapping[str, object],
    sources: Mapping[str, Path],
    output_directory: Path,
) -> ProcessDrillBundlePaths:
    if set(sources) != set(PROCESS_DRILL_ARTIFACTS):
        raise ValueError("process drill sources differ from the required artifact contract")
    report_id = report.get("report_id")
    repository = report.get("repository")
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise ValueError("process drill report requires a SHA-256 report_id")
    if not isinstance(repository, Mapping) or not isinstance(repository.get("git_sha"), str):
        raise ValueError("process drill report requires repository identity")
    git_sha = str(repository["git_sha"])
    target = output_directory / f"process-drills-{git_sha[:12]}-{report_id[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"process drill bundle already exists: {target}")
    for name, path in sources.items():
        if Path(name).name != name or not path.is_file() or path.is_symlink():
            raise ValueError(f"process drill source is invalid: {name}")

    with tempfile.TemporaryDirectory(prefix=".maais-process-drills-", dir=output_directory) as tmp:
        temporary = Path(tmp)
        report_path = temporary / "process-drills.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for name, source in sources.items():
            shutil.copy2(source, temporary / name)
        artifacts = (report_path, *(temporary / name for name in PROCESS_DRILL_ARTIFACTS))
        manifest_path = temporary / "bundle-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "process_drill_bundle_schema_version": 1,
                    "report_id": report_id,
                    "artifacts": {
                        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                        for path in artifacts
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    return ProcessDrillBundlePaths(
        directory=target,
        report_path=target / "process-drills.json",
        manifest_path=target / "bundle-manifest.json",
    )


def load_verified_process_drills(directory: Path) -> tuple[dict[str, object], bool]:
    report_path = directory / "process-drills.json"
    manifest_path = directory / "bundle-manifest.json"
    report_value = json.loads(report_path.read_text(encoding="utf-8"))
    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(report_value, dict) or not isinstance(manifest_value, dict):
        raise TypeError("process drill bundle files must contain JSON objects")
    artifacts = manifest_value.get("artifacts")
    if not isinstance(artifacts, dict):
        return cast(dict[str, object], report_value), False
    expected_artifacts = {"process-drills.json", *PROCESS_DRILL_ARTIFACTS}
    expected_directory = expected_artifacts | {"bundle-manifest.json"}
    entries = tuple(directory.iterdir())
    verified = (
        manifest_value.get("process_drill_bundle_schema_version") == 1
        and manifest_value.get("report_id") == report_value.get("report_id")
        and set(artifacts) == expected_artifacts
        and {path.name for path in entries} == expected_directory
        and all(path.is_file() and not path.is_symlink() for path in entries)
    )
    for filename in expected_artifacts:
        identity = artifacts.get(filename)
        path = directory / filename
        if (
            not isinstance(identity, Mapping)
            or not path.is_file()
            or identity.get("sha256") != _sha256(path)
            or identity.get("bytes") != path.stat().st_size
        ):
            verified = False
    return cast(dict[str, object], report_value), verified


def freeze_process_drill_evidence(
    *,
    manifest_path: Path,
    repository_root: Path,
    dashboard_baseline_path: Path,
    dashboard_recovery_path: Path,
    dashboard_after_path: Path,
    worker_baseline_path: Path,
    worker_recovery_path: Path,
    worker_after_path: Path,
    output_directory: Path,
    generated_at: datetime,
) -> tuple[ProcessDrillBundlePaths, dict[str, object]]:
    """Load raw drill records, evaluate them, and freeze their immutable bundle."""
    source_paths = {
        "dashboard-baseline.json": dashboard_baseline_path,
        "dashboard-recovery.json": dashboard_recovery_path,
        "dashboard-after.json": dashboard_after_path,
        "worker-baseline.json": worker_baseline_path,
        "worker-recovery.json": worker_recovery_path,
        "worker-after.json": worker_after_path,
    }
    values: dict[str, dict[str, object]] = {}
    for name, path in source_paths.items():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError(f"process drill artifact must contain a JSON object: {name}")
        values[name] = cast(dict[str, object], value)
    manifest = load_manifest_file(manifest_path)
    repository = capture_repository_identity(repository_root)
    report = evaluate_process_drills(
        manifest=manifest,
        manifest_path=manifest_path,
        repository=repository,
        dashboard_baseline=values["dashboard-baseline.json"],
        dashboard_recovery=values["dashboard-recovery.json"],
        dashboard_after=values["dashboard-after.json"],
        worker_baseline=values["worker-baseline.json"],
        worker_recovery=values["worker-recovery.json"],
        worker_after=values["worker-after.json"],
        generated_at=generated_at,
    )
    paths = write_process_drill_bundle(report, source_paths, output_directory)
    return paths, report
