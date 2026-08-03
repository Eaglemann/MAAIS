from __future__ import annotations

import os
import shlex
import stat
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


def test_signal_process_tree_is_idempotent_after_the_root_already_exited() -> None:
    result = _run_bash(
        """
set -euo pipefail
ps() { return 1; }
paper_signal_process_tree 50560
printf 'cleanup-continued\n'
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["cleanup-continued"]


def test_http_readiness_rejects_a_stale_service_when_launched_process_is_dead() -> None:
    result = _run_bash(
        """
kill() { return 1; }
curl() { return 0; }
sleep() { return 0; }
paper_wait_http_process 50560 http://127.0.0.1:8000/api/v1/health 3
"""
    )

    assert result.returncode == 1


def test_http_endpoint_guard_rejects_an_existing_healthy_listener() -> None:
    result = _run_bash(
        """
curl() { return 0; }
paper_require_http_endpoint_free http://127.0.0.1:8000/api/v1/health
"""
    )

    assert result.returncode == 1
    assert "already has a healthy listener" in result.stderr


def test_start_window_calculates_the_next_safe_minute_boundary() -> None:
    result = _run_bash(
        """
for second in 00 05 06 52 59; do
  printf '%s=%s\n' "$second" "$(paper_start_wait_seconds "$second" 5)"
done
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "00=0",
        "05=0",
        "06=54",
        "52=8",
        "59=1",
    ]


def test_start_window_waits_when_the_current_minute_is_too_close_to_close() -> None:
    result = _run_bash(
        """
MAAIS_RUN_PURPOSE=soak
date() { printf '52\n'; }
sleep() { printf 'slept=%s\n' "$1"; }
paper_wait_for_start_window 5
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["slept=8"]
    assert "aligning paper activation to a safe minute boundary" in result.stderr


def test_fault_window_waits_until_after_the_close_cycle_and_before_next_close() -> None:
    result = _run_bash(
        """
for second in 00 09 10 15 16 59; do
  printf '%s=%s\n' "$second" "$(paper_minute_window_wait_seconds "$second" 10 15)"
done
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "00=10",
        "09=1",
        "10=0",
        "15=0",
        "16=54",
        "59=11",
    ]


def test_seven_day_start_waits_for_the_next_berlin_midnight() -> None:
    result = _run_bash(
        """
slept=false
date() {
  if [[ "${slept}" == false ]]; then
    printf '23:59:58\n'
  else
    printf '00:00:00\n'
  fi
}
sleep() {
  printf 'slept=%s\n' "$1"
  slept=true
}
paper_wait_for_start_window 5
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["slept=2"]
    assert "aligning seven-day activation to Berlin midnight" in result.stderr


def test_seven_day_start_accepts_only_the_first_five_seconds_at_midnight() -> None:
    result = _run_bash(
        """
date() { printf '00:00:03\n'; }
sleep() { printf 'unexpected-sleep=%s\n' "$1"; return 99; }
paper_wait_for_start_window 5
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_seven_day_start_fails_closed_outside_the_bounded_midnight_window() -> None:
    result = _run_bash(
        """
date() { printf '00:00:06\n'; }
sleep() { printf 'unexpected-sleep=%s\n' "$1"; return 99; }
paper_wait_for_start_window 5
"""
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "must begin at Berlin midnight" in result.stderr


def test_seven_day_start_never_waits_more_than_ten_minutes() -> None:
    result = _run_bash(
        """
date() { printf '23:49:59\n'; }
sleep() { printf 'unexpected-sleep=%s\n' "$1"; return 99; }
paper_wait_for_start_window 5
"""
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "within 10 minutes before 00:00" in result.stderr


def test_seven_day_start_fails_if_the_wait_wakes_after_the_start_window() -> None:
    result = _run_bash(
        """
slept=false
date() {
  if [[ "${slept}" == false ]]; then
    printf '23:59:59\n'
  else
    printf '00:00:06\n'
  fi
}
sleep() { slept=true; }
paper_wait_for_start_window 5
"""
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "missed the Berlin midnight start window" in result.stderr


def test_seven_day_launcher_requires_an_explicit_soak_readiness_bundle() -> None:
    result = subprocess.run(
        [
            str(REPOSITORY_ROOT / "scripts" / "start-paper-week.sh"),
            "manifest.json",
            "restore-verification.json",
            "qualification-bundle",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "SOAK_READINESS_BUNDLE" in result.stderr


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


def test_control_token_is_created_privately_and_reused_without_rotation(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "mission-control.token"
    result = _run_bash(
        f"""
openssl() {{ printf '%064d\n' 0; }}
paper_ensure_control_token {shlex.quote(str(token_file))}
printf 'first=%s\n' "$(cat {shlex.quote(str(token_file))})"
openssl() {{ printf 'must-not-rotate\n'; return 9; }}
paper_ensure_control_token {shlex.quote(str(token_file))}
printf 'second=%s\n' "$(cat {shlex.quote(str(token_file))})"
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"first={'0' * 64}",
        f"second={'0' * 64}",
    ]
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_control_token_validation_rejects_group_readable_file(tmp_path: Path) -> None:
    token_file = tmp_path / "mission-control.token"
    token_file.write_text(f"{'a1' * 32}\n", encoding="ascii")
    token_file.chmod(0o640)

    result = _run_bash(f"paper_ensure_control_token {shlex.quote(str(token_file))}")

    assert result.returncode == 1
    assert "permissions must be 600" in result.stderr


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


def test_operation_lock_rejects_a_concurrent_live_owner(tmp_path: Path) -> None:
    lock_directory = tmp_path / "daily.lock"
    result = _run_bash(
        f"""
paper_acquire_operation_lock {shlex.quote(str(lock_directory))} daily-close
paper_acquire_operation_lock {shlex.quote(str(lock_directory))} daily-close
"""
    )

    assert result.returncode == 1
    assert "operation lock is already held" in result.stderr


def test_operation_lock_preserves_stale_evidence_and_recovers(tmp_path: Path) -> None:
    lock_directory = tmp_path / "daily.lock"
    lock_directory.mkdir()
    (lock_directory / "owner.pid").write_text("99999999\n", encoding="utf-8")

    result = _run_bash(
        f"""
paper_acquire_operation_lock {shlex.quote(str(lock_directory))} daily-close
printf 'owner=%s\n' "$(cat {shlex.quote(str(lock_directory / "owner.pid"))})"
paper_release_operation_lock {shlex.quote(str(lock_directory))}
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("owner=")
    assert not lock_directory.exists()
    assert len(list(tmp_path.glob("daily.lock.stale.*"))) == 1
