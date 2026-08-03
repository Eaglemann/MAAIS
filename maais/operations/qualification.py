"""Immutable verification evidence for an exact paper-candidate commit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from sqlalchemy.engine import make_url

from maais.domain.json import content_hash, to_json_data
from maais.experiments.prepare import RepositoryIdentity, capture_repository_identity

UTC = timezone.utc
QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_MAX_AGE = timedelta(hours=24)
Runner = Callable[..., subprocess.CompletedProcess[str]]
REQUIRED_QUALIFICATION_CHECKS = (
    "migration",
    "backend_branch_coverage",
    "golden_replay",
    "fault_injection",
    "ruff_format",
    "ruff_lint",
    "pyright",
    "python_dependency_audit",
    "secret_scan",
    "execution_safety",
    "frontend_install",
    "frontend_dependency_audit",
    "frontend_tests",
    "frontend_typecheck",
    "frontend_build",
    "real_browser",
)


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


@dataclass(frozen=True, slots=True)
class QualificationCheckResult:
    name: str
    command: tuple[str, ...]
    exit_code: int
    duration_seconds: float
    output_file: str
    output_sha256: str
    output_bytes: int

    def __post_init__(self) -> None:
        if not self.name or not self.command:
            raise ValueError("qualification check name and command are required")
        if self.duration_seconds < 0 or self.output_bytes < 0:
            raise ValueError("qualification result counters cannot be negative")
        if Path(self.output_file).name != self.output_file or not self.output_file.endswith(".log"):
            raise ValueError("qualification output must be a flat log filename")
        if len(self.output_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.output_sha256
        ):
            raise ValueError("qualification output hash must be SHA-256")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.exit_code == 0,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "output_file": self.output_file,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
        }


@dataclass(frozen=True, slots=True)
class QualificationBundlePaths:
    directory: Path
    report_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class QualificationCommand:
    name: str
    command: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9_]+", self.name) is None:
            raise ValueError("qualification command name must be lowercase snake case")
        if not self.command or any(not item for item in self.command):
            raise ValueError("qualification command arguments must be nonempty")


def run_qualification_checks(
    commands: Sequence[QualificationCommand],
    log_directory: Path,
    *,
    repository_root: Path | None = None,
    runner: Runner = subprocess.run,
) -> tuple[QualificationCheckResult, ...]:
    """Run every gate and retain a complete, hash-addressed log even after failures."""
    log_directory.mkdir(parents=True, exist_ok=False)
    base_environment = os.environ.copy()
    results: list[QualificationCheckResult] = []
    for item in commands:
        started = time.monotonic()
        environment = {**base_environment, **dict(item.environment)}
        try:
            completed = runner(
                item.command,
                cwd=repository_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            exit_code = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except OSError as exc:
            exit_code = 127
            stdout = ""
            stderr = f"{type(exc).__name__}: {exc}"
        duration = time.monotonic() - started
        filename = f"{len(results) + 1:02d}-{item.name}.log"
        log_path = log_directory / filename
        rendered_command = " ".join(item.command)
        log_path.write_text(
            f"command: {rendered_command}\n"
            f"exit_code: {exit_code}\n"
            f"duration_seconds: {duration:.6f}\n"
            "\n[stdout]\n"
            f"{stdout}"
            "\n[stderr]\n"
            f"{stderr}",
            encoding="utf-8",
        )
        results.append(
            QualificationCheckResult(
                name=item.name,
                command=item.command,
                exit_code=exit_code,
                duration_seconds=round(duration, 6),
                output_file=filename,
                output_sha256=_sha256(log_path),
                output_bytes=log_path.stat().st_size,
            )
        )
    return tuple(results)


def build_qualification_report(
    *,
    repository_before: RepositoryIdentity,
    repository_after: RepositoryIdentity,
    results: Sequence[QualificationCheckResult],
    generated_at: datetime,
) -> dict[str, object]:
    if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(0):
        raise ValueError("qualification generated_at must be UTC-aware")
    names = tuple(result.name for result in results)
    exact_checks = names == REQUIRED_QUALIFICATION_CHECKS
    repository_unchanged = (
        repository_before == repository_after
        and repository_before.worktree_hash is None
        and repository_after.worktree_hash is None
    )
    checks = [result.to_dict() for result in results]
    base = {
        "qualification_schema_version": QUALIFICATION_SCHEMA_VERSION,
        "generated_at": generated_at,
        "passed": (
            exact_checks
            and repository_unchanged
            and all(result.exit_code == 0 for result in results)
        ),
        "repository_unchanged": repository_unchanged,
        "required_checks": list(REQUIRED_QUALIFICATION_CHECKS),
        "repository": _repository_payload(repository_before),
        "repository_after": _repository_payload(repository_after),
        "checks": checks,
    }
    normalized = to_json_data(base)
    if not isinstance(normalized, dict):
        raise TypeError("qualification report must normalize to an object")
    report = cast(dict[str, object], normalized)
    report["report_id"] = content_hash(report)
    return report


def qualification_evidence_passes(
    report: Mapping[str, object],
    *,
    repository: RepositoryIdentity,
    bundle_verified: bool,
    evaluated_at: datetime,
    maximum_age: timedelta = QUALIFICATION_MAX_AGE,
) -> bool:
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() != timedelta(0):
        raise ValueError("qualification evaluation time must be UTC-aware")
    if maximum_age <= timedelta(0):
        raise ValueError("qualification maximum age must be positive")
    generated_value = report.get("generated_at")
    if not isinstance(generated_value, str):
        return False
    try:
        generated_at = datetime.fromisoformat(generated_value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(0):
        return False
    age = evaluated_at - generated_at
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        return False
    names = [cast(Mapping[str, object], check).get("name") for check in checks]
    report_without_id = {key: value for key, value in report.items() if key != "report_id"}
    return (
        bundle_verified
        and report.get("qualification_schema_version") == QUALIFICATION_SCHEMA_VERSION
        and report.get("passed") is True
        and report.get("repository_unchanged") is True
        and report.get("repository") == _repository_payload(repository)
        and report.get("repository_after") == _repository_payload(repository)
        and report.get("required_checks") == list(REQUIRED_QUALIFICATION_CHECKS)
        and names == list(REQUIRED_QUALIFICATION_CHECKS)
        and all(cast(Mapping[str, object], check).get("passed") is True for check in checks)
        and report.get("report_id") == content_hash(report_without_id)
        and timedelta(0) <= age <= maximum_age
    )


def write_qualification_bundle(
    report: Mapping[str, object],
    log_directory: Path,
    output_directory: Path,
) -> QualificationBundlePaths:
    report_id = report.get("report_id")
    repository = report.get("repository")
    checks = report.get("checks")
    if not isinstance(report_id, str) or len(report_id) != 64:
        raise ValueError("qualification report requires a SHA-256 report_id")
    if not isinstance(repository, Mapping) or not isinstance(repository.get("git_sha"), str):
        raise ValueError("qualification report requires repository identity")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        raise ValueError("qualification report requires check results")

    git_sha = str(repository["git_sha"])
    target = output_directory / f"qualification-{git_sha[:12]}-{report_id[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"qualification bundle already exists: {target}")

    log_paths: list[Path] = []
    for raw_check in checks:
        check = cast(Mapping[str, object], raw_check)
        filename = check.get("output_file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("qualification check output filename is invalid")
        path = log_directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"qualification log not found: {path}")
        if _sha256(path) != check.get("output_sha256") or path.stat().st_size != check.get(
            "output_bytes"
        ):
            raise ValueError(f"qualification log identity differs: {filename}")
        log_paths.append(path)

    with tempfile.TemporaryDirectory(prefix=".maais-qualification-", dir=output_directory) as tmp:
        temporary = Path(tmp)
        report_path = temporary / "qualification.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for source in log_paths:
            shutil.copy2(source, temporary / source.name)
        artifacts = (report_path, *(temporary / path.name for path in log_paths))
        manifest_path = temporary / "bundle-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "qualification_bundle_schema_version": 1,
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
    return QualificationBundlePaths(
        directory=target,
        report_path=target / "qualification.json",
        manifest_path=target / "bundle-manifest.json",
    )


def load_verified_qualification(directory: Path) -> tuple[dict[str, object], bool]:
    report_path = directory / "qualification.json"
    manifest_path = directory / "bundle-manifest.json"
    report_value = json.loads(report_path.read_text(encoding="utf-8"))
    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(report_value, dict) or not isinstance(manifest_value, dict):
        raise TypeError("qualification bundle files must contain JSON objects")
    artifacts = manifest_value.get("artifacts")
    checks = report_value.get("checks")
    if not isinstance(artifacts, dict) or not isinstance(checks, list):
        return cast(dict[str, object], report_value), False
    expected_names = {"qualification.json"}
    for raw_check in checks:
        if not isinstance(raw_check, Mapping) or not isinstance(raw_check.get("output_file"), str):
            return cast(dict[str, object], report_value), False
        expected_names.add(str(raw_check["output_file"]))
    expected_directory_names = expected_names | {"bundle-manifest.json"}
    actual_entries = tuple(directory.iterdir())
    verified = (
        manifest_value.get("qualification_bundle_schema_version") == 1
        and manifest_value.get("report_id") == report_value.get("report_id")
        and set(artifacts) == expected_names
        and {path.name for path in actual_entries} == expected_directory_names
        and all(path.is_file() and not path.is_symlink() for path in actual_entries)
    )
    for filename in expected_names:
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


def default_qualification_commands(
    repository_root: Path,
    *,
    test_database_url: str,
) -> tuple[QualificationCommand, ...]:
    """Return the explicit fresh-evidence contract for one clean commit."""
    parsed = make_url(test_database_url)
    if not parsed.drivername.startswith("postgresql") or not parsed.database:
        raise ValueError("qualification requires a PostgreSQL test database URL")
    if not parsed.database.endswith("_test"):
        raise ValueError("qualification database name must end in _test")
    tracked = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=repository_root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    tracked_paths = tuple(item.decode("utf-8") for item in tracked if item)
    database_environment = {
        "DATABASE_URL": test_database_url,
        "MAAIS_TEST_DATABASE_URL": test_database_url,
    }
    coverage = (
        "uv",
        "run",
        "pytest",
        "-q",
        "--cov=maais.execution.paper",
        "--cov=maais.db.repositories",
        "--cov=maais.risk",
        "--cov=maais.orchestration",
        "--cov=maais.market_data.recovery",
        "--cov-branch",
        "--cov-report=term:skip-covered",
        "--cov-fail-under=80",
    )
    fault_paths = (
        "tests/faults",
        "tests/unit/market_data/test_binance_websocket_connector.py",
        "tests/unit/market_data/test_gap_recovery.py",
        "tests/unit/market_data/test_frame_builder.py",
        "tests/unit/market_data/test_integrity_state_machine.py",
        "tests/integration/faults",
        "tests/integration/test_recovery_store.py",
        "tests/integration/test_operational_state_repository.py",
        "tests/integration/test_decision_lineage.py",
    )
    commands = (
        QualificationCommand(
            "migration",
            ("uv", "run", "alembic", "upgrade", "head"),
            database_environment,
        ),
        QualificationCommand("backend_branch_coverage", coverage, database_environment),
        QualificationCommand(
            "golden_replay",
            ("uv", "run", "pytest", "tests/replay/test_golden_paper_sequence.py", "-q"),
            database_environment,
        ),
        QualificationCommand(
            "fault_injection",
            ("uv", "run", "pytest", *fault_paths, "-q"),
            database_environment,
        ),
        QualificationCommand("ruff_format", ("uv", "run", "ruff", "format", "--check", ".")),
        QualificationCommand("ruff_lint", ("uv", "run", "ruff", "check", ".")),
        QualificationCommand("pyright", ("uv", "run", "pyright")),
        QualificationCommand("python_dependency_audit", ("uv", "run", "pip-audit")),
        QualificationCommand(
            "secret_scan",
            (
                "uv",
                "run",
                "detect-secrets-hook",
                "--baseline",
                ".secrets.baseline",
                "--exclude-files",
                r"(^uv\.lock$|^\.superpowers/)",
                *tracked_paths,
            ),
        ),
        QualificationCommand(
            "execution_safety",
            ("uv", "run", "pytest", "tests/test_execution_safety.py", "-q"),
        ),
        QualificationCommand("frontend_install", ("npm", "--prefix", "dashboard", "ci")),
        QualificationCommand(
            "frontend_dependency_audit",
            ("npm", "--prefix", "dashboard", "audit", "--audit-level=high"),
        ),
        QualificationCommand("frontend_tests", ("npm", "--prefix", "dashboard", "test")),
        QualificationCommand(
            "frontend_typecheck",
            ("npm", "--prefix", "dashboard", "run", "typecheck"),
        ),
        QualificationCommand(
            "frontend_build",
            ("npm", "--prefix", "dashboard", "run", "build"),
        ),
        QualificationCommand("real_browser", (str(repository_root / "scripts/browser-smoke.sh"),)),
    )
    if tuple(command.name for command in commands) != REQUIRED_QUALIFICATION_CHECKS:
        raise RuntimeError("default qualification commands differ from the required contract")
    return commands


def run_candidate_qualification(
    *,
    repository_root: Path,
    output_directory: Path,
    test_database_url: str,
    runner: Runner = subprocess.run,
) -> tuple[QualificationBundlePaths, dict[str, object]]:
    root = repository_root.resolve(strict=True)
    before = capture_repository_identity(root)
    if before.worktree_hash is not None:
        raise ValueError("qualification requires a clean committed worktree")
    commands = default_qualification_commands(root, test_database_url=test_database_url)
    output_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".maais-qualification-work-", dir=output_directory
    ) as temporary:
        log_directory = Path(temporary) / "logs"
        results = run_qualification_checks(
            commands,
            log_directory,
            repository_root=root,
            runner=runner,
        )
        after = capture_repository_identity(root)
        report = build_qualification_report(
            repository_before=before,
            repository_after=after,
            results=results,
            generated_at=datetime.now(UTC),
        )
        paths = write_qualification_bundle(report, log_directory, output_directory)
    return paths, report
