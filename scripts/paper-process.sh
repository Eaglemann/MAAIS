#!/usr/bin/env bash

# Send SIGINT to the complete worker tree without broad name-based matching.
# A tmux-launched worker leads its own process group. A worker launched by another
# supervisor can instead be a uv wrapper with the Python process beneath it.
PAPER_TMUX_PANE_PID=""
PAPER_OPERATION_LOCK_DIR=""

paper_curl() {
  local connect_timeout="${MAAIS_HTTP_CONNECT_TIMEOUT_SECONDS:-2}"
  local maximum_time="${MAAIS_HTTP_MAX_TIME_SECONDS:-10}"

  if [[ ! "${connect_timeout}" =~ ^[1-9][0-9]*$ \
    || ! "${maximum_time}" =~ ^[1-9][0-9]*$ ]]; then
    echo "MAAIS HTTP timeouts must be positive integer seconds" >&2
    return 64
  fi

  curl \
    --connect-timeout "${connect_timeout}" \
    --max-time "${maximum_time}" \
    "$@"
}

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

paper_start_postgres() {
  local context="$1"

  paper_docker_compose "${context}" up -d --wait --pull never postgres
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

paper_assert_recorded_configured_postgres_identity() {
  local recorded_identity="$1"
  local configured_identity

  if [[ ! "${recorded_identity}" =~ ^[0-9]+$ ]]; then
    echo "recorded candidate PostgreSQL system_identifier is invalid" >&2
    return 1
  fi
  configured_identity="$(paper_configured_postgres_identity)" || return 1
  if [[ ! "${configured_identity}" =~ ^[0-9]+$ ]]; then
    echo "configured PostgreSQL returned an invalid system_identifier" >&2
    return 1
  fi
  if [[ "${configured_identity}" != "${recorded_identity}" ]]; then
    echo "configured PostgreSQL system_identifier ${configured_identity} differs from recorded candidate system_identifier ${recorded_identity}" >&2
    return 1
  fi
  printf '%s\n' "${configured_identity}"
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
  if paper_curl --silent --show-error --fail --max-time 1 "${health_url}" >/dev/null 2>&1; then
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
    if paper_curl --silent --show-error --fail "${health_url}" >/dev/null 2>&1; then
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

paper_minute_window_wait_seconds() {
  local current_second="$1"
  local minimum_second="$2"
  local maximum_second="$3"
  local current_value
  local minimum_value
  local maximum_value

  if [[ ! "${current_second}" =~ ^[0-5][0-9]$ \
    || ! "${minimum_second}" =~ ^([0-9]|[1-5][0-9])$ \
    || ! "${maximum_second}" =~ ^([0-9]|[1-5][0-9])$ ]]; then
    echo "paper minute window requires second 00-59 and bounds 0-59" >&2
    return 64
  fi
  current_value=$((10#${current_second}))
  minimum_value=$((10#${minimum_second}))
  maximum_value=$((10#${maximum_second}))
  if ((minimum_value > maximum_value)); then
    echo "paper minute window minimum cannot exceed maximum" >&2
    return 64
  fi
  if ((current_value < minimum_value)); then
    printf '%s\n' "$((minimum_value - current_value))"
    return 0
  fi
  if ((current_value <= maximum_value)); then
    printf '0\n'
    return 0
  fi
  printf '%s\n' "$((60 - current_value + minimum_value))"
}

paper_wait_for_minute_window() {
  local minimum_second="$1"
  local maximum_second="$2"
  local current_second
  local wait_seconds

  current_second="$(date -u +%S)" || return 1
  wait_seconds="$(
    paper_minute_window_wait_seconds \
      "${current_second}" "${minimum_second}" "${maximum_second}"
  )" || return 1
  if ((wait_seconds > 0)); then
    echo "aligning recovery drill fault to the post-close window: wait=${wait_seconds}s" >&2
    sleep "${wait_seconds}"
  fi
}

paper_wait_for_berlin_midnight_start_window() {
  local maximum_start_second="$1"
  local maximum_wait_seconds=600
  local current_time
  local current_hour
  local current_minute
  local current_second
  local seconds_since_midnight
  local wait_seconds

  if [[ ! "${maximum_start_second}" =~ ^([0-9]|1[0-5])$ ]]; then
    echo "Berlin midnight start window requires maximum second 0-15" >&2
    return 64
  fi

  current_time="$(TZ=Europe/Berlin date +%H:%M:%S)" || return 1
  if [[ ! "${current_time}" =~ ^([0-1][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$ ]]; then
    echo "could not read the current Europe/Berlin time" >&2
    return 1
  fi
  IFS=: read -r current_hour current_minute current_second <<<"${current_time}"
  if ((10#${current_hour} == 0 && 10#${current_minute} == 0 \
    && 10#${current_second} <= 10#${maximum_start_second})); then
    return 0
  fi

  seconds_since_midnight=$((
    10#${current_hour} * 3600 + 10#${current_minute} * 60 + 10#${current_second}
  ))
  wait_seconds=$((86400 - seconds_since_midnight))
  if ((wait_seconds > maximum_wait_seconds)); then
    echo "seven-day paper trading must begin at Berlin midnight; prepare the launch within 10 minutes before 00:00" >&2
    return 1
  fi

  echo "aligning seven-day activation to Berlin midnight: wait=${wait_seconds}s" >&2
  sleep "${wait_seconds}" || return 1
  current_time="$(TZ=Europe/Berlin date +%H:%M:%S)" || return 1
  if [[ ! "${current_time}" =~ ^00:00:([0-5][0-9])$ \
    || 10#${BASH_REMATCH[1]} -gt 10#${maximum_start_second} ]]; then
    echo "seven-day paper trading missed the Berlin midnight start window" >&2
    return 1
  fi
}

paper_wait_for_start_window() {
  local maximum_start_second="$1"
  local current_second
  local wait_seconds

  case "${MAAIS_RUN_PURPOSE:-seven_day}" in
    seven_day)
      paper_wait_for_berlin_midnight_start_window "${maximum_start_second}"
      return
      ;;
    process_drill | soak) ;;
    *)
      echo "MAAIS_RUN_PURPOSE must be process_drill, soak, or seven_day" >&2
      return 64
      ;;
  esac

  current_second="$(date -u +%S)" || return 1
  wait_seconds="$(
    paper_start_wait_seconds "${current_second}" "${maximum_start_second}"
  )" || return 1
  if ((wait_seconds > 0)); then
    echo "aligning paper activation to a safe minute boundary: wait=${wait_seconds}s" >&2
    sleep "${wait_seconds}"
  fi
}

paper_assert_timed_run_host_power() {
  local run_purpose="$1"
  local platform
  local power_status
  local battery_percent
  local minimum_battery_percent=50

  if [[ "${run_purpose}" != "process_drill" \
    && "${run_purpose}" != "soak" \
    && "${run_purpose}" != "seven_day" ]]; then
    echo "MAAIS_RUN_PURPOSE must be process_drill, soak, or seven_day" >&2
    return 64
  fi

  platform="$(uname -s)" || return 1
  if [[ "${run_purpose}" == "process_drill" ]]; then
    jq -cn \
      --arg platform "${platform}" \
      '{platform:$platform,required:false,power_source:"not_checked",battery_percent:null,minimum_battery_percent:null}'
    return
  fi
  if [[ "${platform}" != "Darwin" ]]; then
    jq -cn \
      --arg platform "${platform}" \
      '{platform:$platform,required:true,power_source:"not_applicable",battery_percent:null,minimum_battery_percent:null}'
    return
  fi
  if ! command -v pmset >/dev/null 2>&1; then
    echo "timed macOS paper runs require pmset host-power verification" >&2
    return 69
  fi
  power_status="$(pmset -g batt)" || {
    echo "could not read macOS host power status" >&2
    return 1
  }
  if [[ "${power_status}" != *"Now drawing from 'AC Power'"* ]]; then
    echo "a timed macOS paper run requires AC power; battery power cannot preserve an official run" >&2
    return 1
  fi

  battery_percent="$(
    sed -nE 's/.*[^0-9]([0-9]{1,3})%;.*/\1/p' <<<"${power_status}" | head -n 1
  )"
  if [[ "${power_status}" == *"InternalBattery"* && ! "${battery_percent}" =~ ^[0-9]{1,3}$ ]]; then
    echo "could not read the macOS battery reserve" >&2
    return 1
  fi
  if [[ -n "${battery_percent}" ]] \
    && ((10#${battery_percent} < minimum_battery_percent)); then
    echo "macOS battery reserve ${battery_percent}% is below required ${minimum_battery_percent}% for a timed paper run" >&2
    return 1
  fi
  if [[ -z "${battery_percent}" ]]; then
    jq -cn \
      --argjson minimum "${minimum_battery_percent}" \
      '{platform:"macos",required:true,power_source:"ac",battery_percent:null,minimum_battery_percent:$minimum}'
    return
  fi
  jq -cn \
    --argjson battery "${battery_percent}" \
    --argjson minimum "${minimum_battery_percent}" \
    '{platform:"macos",required:true,power_source:"ac",battery_percent:$battery,minimum_battery_percent:$minimum}'
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
    exec caffeinate -ims -w "${worker_pid}"
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
