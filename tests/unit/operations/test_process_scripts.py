from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[3]
PROCESS_HELPER = REPOSITORY_ROOT / "scripts" / "paper-process.sh"


def _run_bash(
    body: str,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(PROCESS_HELPER))}\n{body}",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_signal_worker_targets_process_group_when_worker_is_leader() -> None:
    result = _run_bash(
        """
ps() { printf ' 50560\\n'; }
kill() { printf '%s\\n' "$*"; }
paper_signal_process_tree 50560
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-INT -- -50560"]


def test_signal_worker_targets_child_then_wrapper_without_dedicated_group() -> None:
    result = _run_bash(
        """
ps() { printf '40000\\n'; }
pgrep() {
  if [[ "$1" == "-P" && "$2" == "50560" ]]; then
    printf '50566\\n'
  fi
}
kill() { printf '%s\\n' "$*"; }
paper_signal_process_tree 50560
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["-INT 50566", "-INT 50560"]


def test_sleep_inhibitor_executes_caffeinate_for_worker_pid(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_caffeinate = fake_bin / "caffeinate"
    fake_caffeinate.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_caffeinate.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = _run_bash(
        "paper_run_sleep_inhibitor 50560",
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "-im -w 50560"


def test_tmux_session_start_records_exact_pane_pid() -> None:
    result = _run_bash(
        """
tmux() {
  if [[ "$1" == "new-session" ]]; then
    printf 'start:%s\\n' "$*"
  elif [[ "$1" == "list-panes" ]]; then
    printf '60560\\n'
  fi
}
paper_start_tmux_session maais-worker-test 'exec worker --paper-only'
printf 'pid=%s\\n' "$PAPER_TMUX_PANE_PID"
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "start:new-session -d -s maais-worker-test exec worker --paper-only",
        "pid=60560",
    ]


def test_postgres_route_rejects_a_different_compose_cluster() -> None:
    result = _run_bash(
        """
paper_compose_postgres_identity() { printf '111111\n'; }
paper_configured_postgres_identity() { printf '222222\n'; }
paper_assert_postgres_route orbstack
"""
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "configured PostgreSQL system_identifier 222222 differs from Docker context "
        "orbstack system_identifier 111111"
    )


def test_postgres_route_returns_the_matching_cluster_identity() -> None:
    result = _run_bash(
        """
paper_compose_postgres_identity() { printf '333333\n'; }
paper_configured_postgres_identity() { printf '333333\n'; }
paper_assert_postgres_route desktop-linux
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "333333"


def test_explicit_docker_context_is_preserved_without_using_global_context() -> None:
    environment = os.environ.copy()
    environment["MAAIS_DOCKER_CONTEXT"] = "desktop-linux"
    result = _run_bash(
        """
docker() { printf 'global context must not be queried\n' >&2; return 9; }
paper_resolve_docker_context
""",
        environment=environment,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "desktop-linux"
    assert result.stderr == ""


def test_recorded_postgres_route_rejects_cluster_replacement() -> None:
    result = _run_bash(
        """
paper_assert_postgres_route() { printf '444444\n'; }
paper_assert_recorded_postgres_route desktop-linux 555555
"""
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == (
        "configured PostgreSQL system_identifier 444444 differs from recorded "
        "candidate system_identifier 555555"
    )


def test_recorded_postgres_route_returns_unchanged_identity() -> None:
    result = _run_bash(
        """
paper_assert_postgres_route() { printf '666666\n'; }
paper_assert_recorded_postgres_route desktop-linux 666666
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "666666"
