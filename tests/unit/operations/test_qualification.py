from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maais.experiments.prepare import RepositoryIdentity
from maais.operations.qualification import (
    REQUIRED_QUALIFICATION_CHECKS,
    QualificationCheckResult,
    QualificationCommand,
    build_qualification_report,
    default_qualification_commands,
    load_verified_qualification,
    qualification_evidence_passes,
    run_candidate_qualification,
    run_qualification_checks,
    write_qualification_bundle,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest

NOW = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)


def _repository() -> RepositoryIdentity:
    manifest = _live_manifest(schema_revision="0017", worktree_hash=None)
    return RepositoryIdentity(
        git_sha=manifest.git_sha,
        worktree_hash=None,
        lock_hash=manifest.lock_hash,
        schema_revision=manifest.schema_revision,
        agent_implementation_hashes={
            version.agent_name: version.implementation_hash for version in manifest.agent_versions
        },
    )


def _results() -> tuple[QualificationCheckResult, ...]:
    return tuple(
        QualificationCheckResult(
            name=name,
            command=("verify", name),
            exit_code=0,
            duration_seconds=1.25,
            output_file=f"{name}.log",
            output_sha256="a" * 64,
            output_bytes=42,
        )
        for name in REQUIRED_QUALIFICATION_CHECKS
    )


def test_qualification_requires_every_named_gate_and_exact_clean_identity() -> None:
    repository = _repository()
    report = build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=_results(),
        generated_at=NOW,
    )

    assert report["passed"] is True
    assert report["repository_unchanged"] is True
    assert isinstance(report["report_id"], str)
    assert isinstance(report["checks"], list)
    assert len(report["report_id"]) == 64
    assert {check["name"] for check in report["checks"]} == set(REQUIRED_QUALIFICATION_CHECKS)
    assert qualification_evidence_passes(
        report,
        repository=repository,
        bundle_verified=True,
        evaluated_at=NOW + timedelta(hours=1),
    )


def test_qualification_rejects_failed_missing_stale_tampered_or_changed_evidence() -> None:
    repository = _repository()
    failed = list(_results())
    failed[0] = QualificationCheckResult(
        name=failed[0].name,
        command=failed[0].command,
        exit_code=1,
        duration_seconds=failed[0].duration_seconds,
        output_file=failed[0].output_file,
        output_sha256=failed[0].output_sha256,
        output_bytes=failed[0].output_bytes,
    )
    report = build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=failed,
        generated_at=NOW,
    )
    missing = build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=_results()[:-1],
        generated_at=NOW,
    )
    reordered = build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=tuple(reversed(_results())),
        generated_at=NOW,
    )

    assert report["passed"] is False
    assert missing["passed"] is False
    assert reordered["passed"] is False
    assert not qualification_evidence_passes(
        report,
        repository=repository,
        bundle_verified=True,
        evaluated_at=NOW + timedelta(hours=1),
    )

    good = build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=_results(),
        generated_at=NOW,
    )
    tampered = {**good, "passed": False}
    changed = RepositoryIdentity(
        git_sha="f" * 40,
        worktree_hash=None,
        lock_hash=repository.lock_hash,
        schema_revision=repository.schema_revision,
        agent_implementation_hashes=repository.agent_implementation_hashes,
    )
    assert not qualification_evidence_passes(
        tampered,
        repository=repository,
        bundle_verified=True,
        evaluated_at=NOW + timedelta(hours=1),
    )
    assert not qualification_evidence_passes(
        good,
        repository=changed,
        bundle_verified=True,
        evaluated_at=NOW + timedelta(hours=1),
    )
    assert not qualification_evidence_passes(
        good,
        repository=repository,
        bundle_verified=False,
        evaluated_at=NOW + timedelta(hours=1),
    )
    assert not qualification_evidence_passes(
        good,
        repository=repository,
        bundle_verified=True,
        evaluated_at=NOW + timedelta(hours=25),
    )


def test_qualification_bundle_verifies_every_artifact_hash(tmp_path: Path) -> None:
    repository = _repository()
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    results = []
    for result in _results():
        path = log_directory / result.output_file
        path.write_text("verified\n", encoding="utf-8")
        results.append(
            QualificationCheckResult(
                name=result.name,
                command=result.command,
                exit_code=0,
                duration_seconds=result.duration_seconds,
                output_file=result.output_file,
                output_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                output_bytes=path.stat().st_size,
            )
        )
    report = build_qualification_report(
        repository_before=repository,
        repository_after=repository,
        results=results,
        generated_at=NOW,
    )
    paths = write_qualification_bundle(report, log_directory, tmp_path / "bundles")

    loaded, verified = load_verified_qualification(paths.directory)

    assert verified is True
    assert loaded == report
    with pytest.raises(FileExistsError):
        write_qualification_bundle(report, log_directory, tmp_path / "bundles")

    unexpected = paths.directory / "unexpected.txt"
    unexpected.write_text("not part of the bundle\n", encoding="utf-8")
    _loaded, verified = load_verified_qualification(paths.directory)
    assert verified is False
    unexpected.unlink()
    _loaded, verified = load_verified_qualification(paths.directory)
    assert verified is True

    first_log = next(paths.directory.glob("*.log"))
    first_log.write_text("tampered\n", encoding="utf-8")
    loaded, verified = load_verified_qualification(paths.directory)
    assert loaded == report
    assert verified is False


def test_qualification_command_runner_records_all_outputs_and_continues(tmp_path: Path) -> None:
    commands = (
        QualificationCommand("first", ("verify", "first")),
        QualificationCommand("second", ("verify", "second")),
    )

    def runner(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        code = 0 if command[-1] == "first" else -9
        return subprocess.CompletedProcess(command, code, stdout=f"out:{command[-1]}", stderr="err")

    log_directory = tmp_path / "logs"
    results = run_qualification_checks(commands, log_directory, runner=runner)

    assert [result.exit_code for result in results] == [0, -9]
    assert [result.name for result in results] == ["first", "second"]
    for result in results:
        log = log_directory / result.output_file
        assert log.is_file()
        assert hashlib.sha256(log.read_bytes()).hexdigest() == result.output_sha256


def test_default_qualification_contract_uses_only_a_separate_test_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = "postgresql+psycopg://localhost:5432/maais_test"

    def fake_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            ("git", "ls-files", "-z"),
            0,
            stdout=b"maais/cli.py\0",
            stderr=b"",
        )

    monkeypatch.setattr("maais.operations.qualification.subprocess.run", fake_git)
    commands = default_qualification_commands(tmp_path, test_database_url=database_url)

    assert tuple(command.name for command in commands) == REQUIRED_QUALIFICATION_CHECKS
    assert database_url not in " ".join(
        argument for command in commands for argument in command.command
    )
    database_commands = [command for command in commands if command.environment]
    assert database_commands
    assert all(command.environment["DATABASE_URL"] == database_url for command in database_commands)

    with pytest.raises(ValueError, match="end in _test"):
        default_qualification_commands(
            tmp_path,
            test_database_url="postgresql+psycopg://localhost/maais",
        )


def test_candidate_qualification_freezes_a_verified_bundle_only_from_a_clean_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository()
    commands = tuple(
        QualificationCommand(name, ("verify", name)) for name in REQUIRED_QUALIFICATION_CHECKS
    )

    monkeypatch.setattr(
        "maais.operations.qualification.capture_repository_identity",
        lambda _root: repository,
    )
    monkeypatch.setattr(
        "maais.operations.qualification.default_qualification_commands",
        lambda *_args, **_kwargs: commands,
    )

    def runner(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="passed\n", stderr="")

    paths, report = run_candidate_qualification(
        repository_root=tmp_path,
        output_directory=tmp_path / "evidence",
        test_database_url="postgresql+psycopg://localhost/maais_test",
        runner=runner,
    )
    loaded, verified = load_verified_qualification(paths.directory)

    assert report["passed"] is True
    assert loaded == report
    assert verified is True

    dirty = RepositoryIdentity(
        git_sha=repository.git_sha,
        worktree_hash="f" * 64,
        lock_hash=repository.lock_hash,
        schema_revision=repository.schema_revision,
        agent_implementation_hashes=repository.agent_implementation_hashes,
    )
    monkeypatch.setattr(
        "maais.operations.qualification.capture_repository_identity",
        lambda _root: dirty,
    )
    with pytest.raises(ValueError, match="clean committed worktree"):
        run_candidate_qualification(
            repository_root=tmp_path,
            output_directory=tmp_path / "dirty-evidence",
            test_database_url="postgresql+psycopg://localhost/maais_test",
            runner=runner,
        )
