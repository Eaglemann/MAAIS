#!/usr/bin/env bash

# Send SIGINT to the complete worker tree without broad name-based matching.
# A tmux-launched worker leads its own process group. A worker launched by the
# operator script can instead be a uv wrapper with the Python process beneath it.
PAPER_SLEEP_INHIBITOR_PID=""
PAPER_SLEEP_INHIBITOR_KIND=""

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

paper_start_sleep_inhibitor() {
  local worker_pid="$1"
  local log_path="$2"

  if [[ ! "${worker_pid}" =~ ^[0-9]+$ ]]; then
    echo "worker PID must be numeric" >&2
    return 64
  fi

  if command -v caffeinate >/dev/null 2>&1; then
    nohup caffeinate -im -w "${worker_pid}" >> "${log_path}" 2>&1 &
    PAPER_SLEEP_INHIBITOR_PID=$!
    PAPER_SLEEP_INHIBITOR_KIND="caffeinate"
    return 0
  fi

  if command -v systemd-inhibit >/dev/null 2>&1; then
    nohup systemd-inhibit \
      --what=sleep:idle \
      --who=MAAIS \
      --why="Timed local paper-trading experiment" \
      --mode=block \
      bash -c 'while kill -0 "$1" 2>/dev/null; do sleep 5; done' bash "${worker_pid}" \
      >> "${log_path}" 2>&1 &
    PAPER_SLEEP_INHIBITOR_PID=$!
    PAPER_SLEEP_INHIBITOR_KIND="systemd-inhibit"
    return 0
  fi

  echo "no supported sleep inhibitor is installed" >&2
  return 69
}
