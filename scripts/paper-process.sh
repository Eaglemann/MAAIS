#!/usr/bin/env bash

# Send SIGINT to the complete worker tree without broad name-based matching.
# A tmux-launched worker leads its own process group. A worker launched by another
# supervisor can instead be a uv wrapper with the Python process beneath it.
PAPER_TMUX_PANE_PID=""

paper_resolve_docker_context() {
  local context="${MAAIS_DOCKER_CONTEXT:-}"

  if [[ -z "${context}" ]]; then
    context="$(docker context show)" || return 1
  fi
  if [[ -z "${context}" || "${context}" == -* || "${context}" =~ [[:space:]] ]]; then
    echo "Docker context must be a nonempty name without whitespace" >&2
    return 64
  fi
  printf '%s\n' "${context}"
}

paper_docker_compose() {
  local context="$1"
  shift
  docker --context "${context}" compose "$@"
}

paper_compose_postgres_identity() {
  local context="$1"
  paper_docker_compose "${context}" exec -T postgres \
    psql -U maais -d maais -Atc \
    'SELECT system_identifier::text FROM pg_control_system()'
}

paper_configured_postgres_identity() {
  uv run maais database-identity | jq -er '.system_identifier'
}

paper_assert_postgres_route() {
  local context="$1"
  local compose_identity
  local configured_identity

  compose_identity="$(paper_compose_postgres_identity "${context}")" || return 1
  configured_identity="$(paper_configured_postgres_identity)" || return 1
  if [[ ! "${compose_identity}" =~ ^[0-9]+$ ]]; then
    echo "Docker context ${context} returned an invalid PostgreSQL system_identifier" >&2
    return 1
  fi
  if [[ ! "${configured_identity}" =~ ^[0-9]+$ ]]; then
    echo "configured PostgreSQL returned an invalid system_identifier" >&2
    return 1
  fi
  if [[ "${compose_identity}" != "${configured_identity}" ]]; then
    echo "configured PostgreSQL system_identifier ${configured_identity} differs from Docker context ${context} system_identifier ${compose_identity}" >&2
    return 1
  fi
  printf '%s\n' "${configured_identity}"
}

paper_assert_recorded_postgres_route() {
  local context="$1"
  local recorded_identity="$2"
  local configured_identity

  if [[ ! "${recorded_identity}" =~ ^[0-9]+$ ]]; then
    echo "recorded candidate PostgreSQL system_identifier is invalid" >&2
    return 1
  fi
  configured_identity="$(paper_assert_postgres_route "${context}")" || return 1
  if [[ "${configured_identity}" != "${recorded_identity}" ]]; then
    echo "configured PostgreSQL system_identifier ${configured_identity} differs from recorded candidate system_identifier ${recorded_identity}" >&2
    return 1
  fi
  printf '%s\n' "${configured_identity}"
}

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
