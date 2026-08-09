from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
VERIFIER = ROOT / "scripts" / "verify-release-candidate.sh"
REQUIRED_JOBS = (
    "quality",
    "test",
    "frontend",
    "frontend-sentry-release",
    "security",
    "postgres-integration",
    "artifact-contract",
    "redaction-contract",
    "container-contract",
    "release-candidate",
)
CURRENT_ACTION_REFERENCES = {
    "actions/checkout": "v6",
    "actions/setup-node": "v7",
    "astral-sh/setup-uv": (
        "c771a70e6277c0a99b617c7a806ffedaca235ff9"  # pragma: allowlist secret
    ),
    "actions/upload-artifact": "v7",
}
PINNED_UV_VERSION = "0.11.16"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(raw: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z0-9][a-z0-9_-]*:\n|\Z)",
        raw,
    )
    assert match is not None, f"workflow job is missing: {name}"
    return match.group(0)


def test_workflow_has_every_release_candidate_gate() -> None:
    raw = _workflow()

    for name in REQUIRED_JOBS:
        _job(raw, name)

    postgres = _job(raw, "postgres-integration")
    assert "--cov-fail-under=80" in postgres
    assert "tests/e2e/test_mission_control_auth.py" in postgres

    migration = postgres
    assert "alembic upgrade 0022" in migration
    assert "alembic downgrade 0018" in migration
    assert migration.count("alembic upgrade 0022") == 2
    assert "tests/integration/test_platform_schema.py" in migration
    assert "tests/integration/test_database_roles.py" in migration

    artifacts = _job(raw, "artifact-contract")
    assert "tests/contracts" in artifacts
    assert "tests/unit/artifacts" in artifacts
    assert "tests/unit/config/test_artifact_settings.py" in artifacts

    redaction = _job(raw, "redaction-contract")
    assert "tests/unit/observability/test_redaction.py" in redaction
    assert "tests/unit/observability/test_structured_logging.py" in redaction
    assert "tests/unit/observability/test_sentry.py" in redaction

    container = _job(raw, "container-contract")
    assert "docker buildx build" in container
    assert "type=oci" in container
    assert "MAAIS_TEST_IMAGE" in container
    assert "tests/container" in container
    assert "secrets." not in container
    assert "docker login" not in container

    release = _job(raw, "release-candidate")
    assert "scripts/verify-release-candidate.sh" in release
    for dependency in (
        "quality",
        "test",
        "frontend",
        "security",
        "postgres-integration",
        "artifact-contract",
        "redaction-contract",
        "container-contract",
    ):
        assert dependency in release


def test_workflow_uses_current_node24_action_references() -> None:
    raw = _workflow()
    actions: dict[str, set[str]] = {}
    for action, version in re.findall(r"uses:\s*([^@\s]+)@([^\s#]+)", raw):
        actions.setdefault(action, set()).add(version)

    for action, expected_reference in CURRENT_ACTION_REFERENCES.items():
        assert actions.get(action) == {expected_reference}
    assert raw.count(
        "uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
    ) == raw.count(f'version: "{PINNED_UV_VERSION}"')


def test_pull_request_jobs_are_read_only_and_receive_no_secrets() -> None:
    raw = _workflow()
    assert "pull_request_target:" not in raw
    pre_jobs = raw.split("jobs:", 1)[0]
    assert re.search(r"(?m)^permissions:\n  contents: read$", pre_jobs)
    assert not re.search(r"(?m)^\s{2,}[a-z-]+: write$", raw)

    secret_jobs = [name for name in REQUIRED_JOBS if "${{ secrets." in _job(raw, name)]
    assert secret_jobs == ["frontend-sentry-release"]
    release = _job(raw, "frontend-sentry-release")
    assert "github.event_name == 'push'" in release
    assert "github.event.repository.fork == false" in release


def test_release_candidate_verifier_is_read_only_and_fail_closed() -> None:
    raw = VERIFIER.read_text(encoding="utf-8")

    for required in (
        "git rev-parse HEAD",
        "git status --porcelain=v1",
        "uv lock --check",
        "uv run alembic heads",
        "scripts/verify_dashboard_assets.py",
        '--expected-release "${expected_sha}"',
        ".github/workflows/ci.yml",
    ):
        assert required in raw
    for job in REQUIRED_JOBS:
        assert job in raw
    for forbidden in (
        "railway ",
        "docker push",
        "docker login",
        "git reset",
        "git clean",
        "git checkout",
        "alembic upgrade",
        "alembic downgrade",
        "kubectl",
        "terraform",
        "gh api",
    ):
        assert forbidden not in raw.lower()


def test_release_candidate_verifier_accepts_exact_clean_commit_and_rejects_drift(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / ".github" / "workflows").mkdir(parents=True)
    (repository / "dashboard" / "dist").mkdir(parents=True)
    shutil.copy2(VERIFIER, repository / "scripts" / VERIFIER.name)
    (repository / "scripts" / "verify_dashboard_assets.py").write_text("", encoding="utf-8")
    (repository / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repository / "dashboard" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (repository / "dashboard" / "dist" / "asset-manifest.json").write_text("{}\n", encoding="utf-8")
    job_definitions = "".join(f"  {name}:\n    runs-on: ubuntu-latest\n" for name in REQUIRED_JOBS)
    (repository / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs:\n" + job_definitions,
        encoding="utf-8",
    )
    bin_directory = repository / "test-bin"
    bin_directory.mkdir()
    _write_executable(
        bin_directory / "uv",
        '#!/bin/sh\nif [ "$1 $2 $3" = "run alembic heads" ]; then echo \'0022 (head)\'; fi\n',
    )
    _write_executable(bin_directory / "python3", "#!/bin/sh\nexit 0\n")

    _run(("git", "init", "-q"), repository)
    _run(("git", "config", "user.name", "MAAIS CI"), repository)
    _run(("git", "config", "user.email", "ci@example.invalid"), repository)
    _run(("git", "add", "."), repository)
    _run(("git", "commit", "-qm", "fixture"), repository)
    sha = _run(("git", "rev-parse", "HEAD"), repository).stdout.strip()
    environment = {
        **os.environ,
        "GITHUB_SHA": sha,
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
    }

    passed = _run((str(repository / "scripts" / VERIFIER.name), sha), repository, environment)

    assert '"outcome":"passed"' in passed.stdout
    (repository / "unexpected.txt").write_text("drift\n", encoding="utf-8")
    failed = subprocess.run(
        (str(repository / "scripts" / VERIFIER.name), sha),
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert "worktree" in failed.stderr.lower()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run(
    command: tuple[str, ...],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
