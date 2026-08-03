#!/usr/bin/env bash

# Send SIGINT to the complete worker tree without broad name-based matching.
# A tmux-launched worker leads its own process group. A worker launched by another
# supervisor can instead be a uv wrapper with the Python process beneath it.
PAPER_TMUX_PANE_PID=""
PAPER_OPERATION_LOCK_DIR=""

paper_file_mode() {
  local path="$1"
  local mode

  if mode="$(stat -f '%Lp' "${path}" 2>/dev/null)"; then
    printf '%s\n' "${mode}"
    return 0
  fi
  stat -c '%a' "${path}"
}

paper_ensure_control_token() {
  local token_path="$1"
  local token_directory
  local temporary
  local mode
  local token
  local byte_count

  if [[ -z "${token_path}" ]]; then
    echo "Mission Control token path is required" >&2
    return 64
  fi
  token_directory="$(dirname "${token_path}")"
  if [[ ! -d "${token_directory}" ]]; then
    echo "Mission Control token directory does not exist: ${token_directory}" >&2
    return 1
  fi
  if [[ ! -e "${token_path}" && ! -L "${token_path}" ]]; then
    if ! command -v openssl >/dev/null 2>&1; then
      echo "openssl is required to generate the Mission Control token" >&2
      return 69
    fi
    temporary="${token_path}.tmp.$$"
    if ! (umask 077; openssl rand -hex 32 > "${temporary}"); then
      rm -f "${temporary}"
      echo "could not generate the Mission Control token" >&2
      return 1
    fi
    chmod 600 "${temporary}"
    if ! ln "${temporary}" "${token_path}" 2>/dev/null; then
      if [[ ! -e "${token_path}" && ! -L "${token_path}" ]]; then
        rm -f "${temporary}"
        echo "could not atomically install the Mission Control token" >&2
        return 1
      fi
    fi
    rm -f "${temporary}"
  fi
  if [[ ! -f "${token_path}" || -L "${token_path}" ]]; then
    echo "Mission Control token must be a regular non-symlink file" >&2
    return 1
  fi
  mode="$(paper_file_mode "${token_path}")" || return 1
  if [[ "${mode}" != "600" ]]; then
    echo "Mission Control token permissions must be 600, found ${mode}" >&2
    return 1
  fi
  IFS= read -r token < "${token_path}" || true
  byte_count="$(wc -c < "${token_path}" | tr -d '[:space:]')"
  if [[ ! "${token}" =~ ^[0-9a-f]{64}$ || "${byte_count}" != "65" ]]; then
    echo "Mission Control token must contain one 64-character lowercase hexadecimal value" >&2
    return 1
  fi
}

paper_acquire_operation_lock() {
  local lock_directory="$1"
  local operation="$2"
  local existing_owner
  local stale_directory
  local attempt

  if [[ -z "${lock_directory}" || -z "${operation}" ]]; then
    echo "operation lock directory and name are required" >&2
    return 64
  fi

  for attempt in 1 2 3; do
    if mkdir "${lock_directory}" 2>/dev/null; then
      printf '%s\n' "$$" > "${lock_directory}/owner.pid"
      printf '%s\n' "${operation}" > "${lock_directory}/operation"
      date -u +%Y-%m-%dT%H:%M:%SZ > "${lock_directory}/acquired-at"
      PAPER_OPERATION_LOCK_DIR="${lock_directory}"
      return 0
    fi

    existing_owner="$(cat "${lock_directory}/owner.pid" 2>/dev/null || true)"
    if [[ ! "${existing_owner}" =~ ^[0-9]+$ ]]; then
      echo "operation lock is present but has no valid owner: ${lock_directory}" >&2
      return 1
    fi
    if kill -0 "${existing_owner}" 2>/dev/null; then
      echo "operation lock is already held by PID ${existing_owner}: ${lock_directory}" >&2
      return 1
    fi

    stale_directory="${lock_directory}.stale.$(date -u +%Y%m%dT%H%M%SZ).${existing_owner}.$$.${attempt}"
    if mv "${lock_directory}" "${stale_directory}" 2>/dev/null; then
      continue
    fi
  done

  echo "could not acquire operation lock after concurrent recovery: ${lock_directory}" >&2
  return 1
}

paper_release_operation_lock() {
  local lock_directory="$1"
  local existing_owner

  existing_owner="$(cat "${lock_directory}/owner.pid" 2>/dev/null || true)"
  if [[ "${existing_owner}" != "$$" ]]; then
    echo "refusing to release an operation lock owned by PID ${existing_owner:-unknown}" >&2
    return 1
  fi
  rm -f \
    "${lock_directory}/owner.pid" \
    "${lock_directory}/operation" \
    "${lock_directory}/acquired-at"
  rmdir "${lock_directory}"
  PAPER_OPERATION_LOCK_DIR=""
}

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

  process_group="$({ ps -o pgid= -p "${root_pid}" 2>/dev/null || true; } | tr -d '[:space:]')"
  if [[ -z "${process_group}" ]]; then
    return 0
  fi
  if [[ "${process_group}" == "${root_pid}" ]]; then
    kill -INT -- "-${process_group}" 2>/dev/null || true
    return 0
  fi

  paper_signal_descendants "${root_pid}"
  kill -INT "${root_pid}" 2>/dev/null || true
}

paper_require_http_endpoint_free() {
  local health_url="$1"

  if [[ -z "${health_url}" ]]; then
    echo "HTTP health URL is required" >&2
    return 64
  fi
  if curl --silent --show-error --fail --max-time 1 "${health_url}" >/dev/null 2>&1; then
    echo "Mission Control endpoint already has a healthy listener: ${health_url}" >&2
    return 1
  fi
}

paper_wait_http_process() {
  local root_pid="$1"
  local health_url="$2"
  local maximum_attempts="$3"
  local attempt

  if [[ ! "${root_pid}" =~ ^[0-9]+$ || -z "${health_url}" \
    || ! "${maximum_attempts}" =~ ^[1-9][0-9]*$ ]]; then
    echo "HTTP process readiness arguments are invalid" >&2
    return 64
  fi

  for ((attempt = 1; attempt <= maximum_attempts; attempt++)); do
    if ! kill -0 "${root_pid}" 2>/dev/null; then
      return 1
    fi
    if curl --silent --show-error --fail "${health_url}" >/dev/null 2>&1; then
      sleep 1
      kill -0 "${root_pid}" 2>/dev/null
      return
    fi
    sleep 1
  done
  return 1
}

paper_start_wait_seconds() {
  local current_second="$1"
  local maximum_start_second="$2"
  local current_value
  local maximum_value

  if [[ ! "${current_second}" =~ ^[0-5][0-9]$ \
    || ! "${maximum_start_second}" =~ ^([0-9]|1[0-5])$ ]]; then
    echo "paper start window requires second 00-59 and maximum 0-15" >&2
    return 64
  fi
  current_value=$((10#${current_second}))
  maximum_value=$((10#${maximum_start_second}))
  if ((current_value <= maximum_value)); then
    printf '0\n'
    return 0
  fi
  printf '%s\n' "$((60 - current_value))"
}

paper_wait_for_start_window() {
  local maximum_start_second="$1"
  local current_second
  local wait_seconds

  current_second="$(date -u +%S)" || return 1
  wait_seconds="$(
    paper_start_wait_seconds "${current_second}" "${maximum_start_second}"
  )" || return 1
  if ((wait_seconds > 0)); then
    echo "aligning paper activation to a safe minute boundary: wait=${wait_seconds}s" >&2
    sleep "${wait_seconds}"
  fi
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
