from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[3]
PROCESS_HELPER = REPOSITORY_ROOT / "scripts" / "paper-process.sh"


def _run_bash(body: str) -> subprocess.CompletedProcess[str]:
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
