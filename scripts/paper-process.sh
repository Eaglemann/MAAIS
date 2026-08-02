#!/usr/bin/env bash

# Send SIGINT to the complete worker tree without broad name-based matching.
# A tmux-launched worker leads its own process group. A worker launched by another
# supervisor can instead be a uv wrapper with the Python process beneath it.
PAPER_TMUX_PANE_PID=""

paper_signal_descendants() {
  local parent_pid="$1"
  local child_pid

  while IFS= read -r child_pid; do
    [[ "${child_pid}" =~ ^[0-9]+$ ]] || continue
    paper_signal_descendants "${child_pid}"
    kill -INT "${child_pid}" 2>/dev/null || true
  done < <(pgrep -P "${parent_pid}" 2>/dev/null || true)
}

paper_signal_process_tree() {
  local root_pid="$1"
  local process_group

  if [[ ! "${root_pid}" =~ ^[0-9]+$ ]]; then
    echo "worker PID must be numeric" >&2
    return 64
  fi

  process_group="$(ps -o pgid= -p "${root_pid}" 2>/dev/null | tr -d '[:space:]')"
  if [[ "${process_group}" == "${root_pid}" ]]; then
    kill -INT -- "-${process_group}" 2>/dev/null || true
    return 0
  fi

  paper_signal_descendants "${root_pid}"
  kill -INT "${root_pid}" 2>/dev/null || true
}

paper_sleep_inhibitor_kind() {
  if command -v caffeinate >/dev/null 2>&1; then
    printf 'caffeinate\n'
    return 0
  fi
  if command -v systemd-inhibit >/dev/null 2>&1; then
    printf 'systemd-inhibit\n'
    return 0
  fi
  echo "no supported sleep inhibitor is installed" >&2
  return 69
}

paper_run_sleep_inhibitor() {
  local worker_pid="$1"

  if [[ ! "${worker_pid}" =~ ^[0-9]+$ ]]; then
    echo "worker PID must be numeric" >&2
    return 64
  fi

  if command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -im -w "${worker_pid}"
  fi

  if command -v systemd-inhibit >/dev/null 2>&1; then
    exec systemd-inhibit \
      --what=sleep:idle \
      --who=MAAIS \
      --why="Timed local paper-trading experiment" \
      --mode=block \
      bash -c 'while kill -0 "$1" 2>/dev/null; do sleep 5; done' bash "${worker_pid}"
  fi

  echo "no supported sleep inhibitor is installed" >&2
  return 69
}

paper_start_tmux_session() {
  local session_name="$1"
  local session_command="$2"
  local pane_pid

  if [[ ! "${session_name}" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "tmux session name is invalid" >&2
    return 64
  fi
  if [[ -z "${session_command}" ]]; then
    echo "tmux session command is required" >&2
    return 64
  fi

  tmux new-session -d -s "${session_name}" "${session_command}"
  pane_pid="$(tmux list-panes -t "${session_name}" -F '#{pane_pid}')"
  if [[ ! "${pane_pid}" =~ ^[0-9]+$ ]]; then
    echo "tmux did not return one pane PID for ${session_name}" >&2
    return 1
  fi
  PAPER_TMUX_PANE_PID="${pane_pid}"
}

paper_stop_tmux_session() {
  local session_name="$1"
  if [[ "${session_name}" =~ ^[A-Za-z0-9_-]+$ ]] \
    && tmux has-session -t "${session_name}" 2>/dev/null; then
    tmux kill-session -t "${session_name}"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ $# -eq 2 && "$1" == "hold-awake" ]]; then
    paper_run_sleep_inhibitor "$2"
  fi
  echo "usage: $0 hold-awake WORKER_PID" >&2
  exit 64
fi
