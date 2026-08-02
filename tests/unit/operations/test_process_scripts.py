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
