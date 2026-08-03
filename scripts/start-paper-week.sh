#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 MANIFEST RESTORE_VERIFICATION [MISSION_CONTROL_PORT]" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
source "${script_dir}/paper-process.sh"
manifest_path="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
restore_path="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
mission_control_port="${3:-8000}"
state_dir="${repository_root}/artifacts/run-state"
log_dir="${state_dir}/logs"
current_state="${state_dir}/current.json"
control_token_file="${state_dir}/mission-control.token"

mkdir -p "${log_dir}" "${repository_root}/artifacts/reports" "${repository_root}/backups"
paper_ensure_control_token "${control_token_file}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required for durable paper-run supervision" >&2
  exit 69
fi

if [[ -f "${current_state}" ]]; then
  existing_worker="$(jq -r '.worker_pid // empty' "${current_state}")"
  if [[ "${existing_worker}" =~ ^[0-9]+$ ]] && kill -0 "${existing_worker}" 2>/dev/null; then
    echo "a paper worker is already running with PID ${existing_worker}" >&2
    exit 1
  fi
fi

cd "${repository_root}"
docker_context="$(paper_resolve_docker_context)"
paper_docker_compose "${docker_context}" up -d --wait postgres
postgres_system_identifier="$(paper_assert_postgres_route "${docker_context}")"
uv run alembic upgrade head

preflight_path="${state_dir}/preflight-$(date -u +%Y%m%dT%H%M%SZ).json"
RUN_MODE=paper_live uv run maais preflight \
  --manifest "${manifest_path}" \
  --restore-verification "${restore_path}" \
  --repository "${repository_root}" \
  --dashboard-dir "${repository_root}/dashboard/dist" \
  > "${preflight_path}"

experiment_id="$(jq -r '.experiment_id' "${manifest_path}")"
session_suffix="${experiment_id%%-*}"
dashboard_session="maais-dashboard-${session_suffix}"
worker_session="maais-worker-${session_suffix}"
awake_session="maais-awake-${session_suffix}"
dashboard_log="${log_dir}/mission-control-${session_suffix}.log"
worker_log="${log_dir}/paper-worker-${session_suffix}.log"
awake_log="${log_dir}/sleep-inhibitor-${session_suffix}.log"
dashboard_pid=""
worker_pid=""
awake_pid=""
start_request_body="${state_dir}/.start-command-${session_suffix}.json"
start_request_config="${state_dir}/.start-command-${session_suffix}.curl"
cleanup_startup() {
  if [[ "${worker_pid}" =~ ^[0-9]+$ ]]; then
    paper_signal_process_tree "${worker_pid}"
  fi
  if [[ "${awake_pid}" =~ ^[0-9]+$ ]]; then
    kill -TERM "${awake_pid}" 2>/dev/null || true
  fi
  if [[ "${dashboard_pid}" =~ ^[0-9]+$ ]]; then
    kill -TERM "${dashboard_pid}" 2>/dev/null || true
  fi
  paper_stop_tmux_session "${awake_session}"
  paper_stop_tmux_session "${worker_session}"
  paper_stop_tmux_session "${dashboard_session}"
  rm -f "${start_request_body}" "${start_request_config}"
}
trap cleanup_startup ERR INT TERM

printf -v dashboard_command \
  'cd %q && exec env RUN_MODE=paper_live ENVIRONMENT=production MISSION_CONTROL_TOKEN_FILE=%q uv run maais mission-control --port %q >> %q 2>&1' \
  "${repository_root}" "${control_token_file}" "${mission_control_port}" "${dashboard_log}"
paper_start_tmux_session "${dashboard_session}" "${dashboard_command}"
dashboard_pid="${PAPER_TMUX_PANE_PID}"

dashboard_ready=false
for _attempt in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${mission_control_port}/api/v1/health" >/dev/null; then
    dashboard_ready=true
    break
  fi
  sleep 1
done
if [[ "${dashboard_ready}" != true ]]; then
  echo "Mission Control did not become healthy; inspect ${dashboard_log}" >&2
  cleanup_startup
  exit 1
fi

printf -v worker_command \
  'cd %q && exec env RUN_MODE=paper_live ENVIRONMENT=production uv run maais paper-live --manifest %q >> %q 2>&1' \
  "${repository_root}" "${manifest_path}" "${worker_log}"
paper_start_tmux_session "${worker_session}" "${worker_command}"
worker_pid="${PAPER_TMUX_PANE_PID}"

worker_ready=false
for _attempt in $(seq 1 90); do
  if ! kill -0 "${worker_pid}" 2>/dev/null; then
    echo "paper worker exited during startup; inspect ${worker_log}" >&2
    cleanup_startup
    exit 1
  fi
  overview="$(curl -fsS "http://127.0.0.1:${mission_control_port}/api/v1/experiments/${experiment_id}/overview" 2>/dev/null || true)"
  if [[ -n "${overview}" ]] && jq -e '.runtime.worker_status == "running" and .runtime.lease_status == "active"' >/dev/null <<<"${overview}"; then
    worker_ready=true
    break
  fi
  sleep 1
done
if [[ "${worker_ready}" != true ]]; then
  echo "paper worker did not acquire a healthy runtime lease; inspect ${worker_log}" >&2
  cleanup_startup
  exit 1
fi

start_idempotency_key="paper-week-start-${experiment_id}"
jq -n \
  --arg idempotency_key "${start_idempotency_key}" \
  '{"command_type":"start","idempotency_key":$idempotency_key,"reason":"start the prepared local paper week","payload":{"source":"start-paper-week.sh"},"confirmation":"CONFIRM START"}' \
  > "${start_request_body}"
control_token="$(<"${control_token_file}")"
umask 077
printf '%s\n' \
  "url = \"http://127.0.0.1:${mission_control_port}/api/v1/experiments/${experiment_id}/commands\"" \
  'request = "POST"' \
  'header = "Content-Type: application/json"' \
  "header = \"Authorization: Bearer ${control_token}\"" \
  "data = \"@${start_request_body}\"" \
  > "${start_request_config}"
unset control_token
start_response="$(curl --silent --show-error --fail --config "${start_request_config}")"
rm -f "${start_request_body}" "${start_request_config}"
start_command_id="$(jq -er '.command_id' <<<"${start_response}")"
command_status=""
experiment_status=""
for _attempt in $(seq 1 60); do
  command_response="$(curl -fsS "http://127.0.0.1:${mission_control_port}/api/v1/commands/${start_command_id}" 2>/dev/null || true)"
  overview="$(curl -fsS "http://127.0.0.1:${mission_control_port}/api/v1/experiments/${experiment_id}/overview" 2>/dev/null || true)"
  if [[ -n "${command_response}" ]]; then
    command_status="$(jq -r '.status // empty' <<<"${command_response}")"
  fi
  if [[ -n "${overview}" ]]; then
    experiment_status="$(jq -r '.experiment.status // empty' <<<"${overview}")"
  fi
  if [[ "${command_status}" == "rejected" ]]; then
    echo "audited START command was rejected: ${command_response}" >&2
    cleanup_startup
    exit 1
  fi
  if [[ "${command_status}" == "completed" && "${experiment_status}" == "running" ]]; then
    break
  fi
  sleep 1
done
if [[ "${command_status}" != "completed" || "${experiment_status}" != "running" ]]; then
  echo "paper worker did not complete the audited START command" >&2
  cleanup_startup
  exit 1
fi

awake_kind="$(paper_sleep_inhibitor_kind)"
printf -v awake_command \
  'exec bash %q hold-awake %q >> %q 2>&1' \
  "${script_dir}/paper-process.sh" "${worker_pid}" "${awake_log}"
paper_start_tmux_session "${awake_session}" "${awake_command}"
awake_pid="${PAPER_TMUX_PANE_PID}"
sleep 1
if ! kill -0 "${awake_pid}" 2>/dev/null; then
  echo "sleep inhibitor exited during startup; inspect ${awake_log}" >&2
  cleanup_startup
  exit 1
fi

temporary_state="${current_state}.tmp"
jq -n \
  --arg experiment_id "${experiment_id}" \
  --arg manifest "${manifest_path}" \
  --arg restore_verification "${restore_path}" \
  --arg preflight "${preflight_path}" \
  --arg control_token_file "${control_token_file}" \
  --arg started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson worker_pid "${worker_pid}" \
  --argjson dashboard_pid "${dashboard_pid}" \
  --argjson awake_pid "${awake_pid}" \
  --arg awake_kind "${awake_kind}" \
  --arg supervisor "tmux" \
  --arg docker_context "${docker_context}" \
  --arg postgres_system_identifier "${postgres_system_identifier}" \
  --arg worker_session "${worker_session}" \
  --arg dashboard_session "${dashboard_session}" \
  --arg awake_session "${awake_session}" \
  --argjson port "${mission_control_port}" \
  '{experiment_id:$experiment_id,manifest:$manifest,restore_verification:$restore_verification,preflight:$preflight,control_token_file:$control_token_file,started_at:$started_at,worker_pid:$worker_pid,dashboard_pid:$dashboard_pid,awake_pid:$awake_pid,awake_kind:$awake_kind,supervisor:$supervisor,docker_context:$docker_context,postgres_system_identifier:$postgres_system_identifier,worker_session:$worker_session,dashboard_session:$dashboard_session,awake_session:$awake_session,mission_control_port:$port}' \
  > "${temporary_state}"
mv "${temporary_state}" "${current_state}"

trap - ERR INT TERM
echo "paper week started: experiment=${experiment_id} worker_pid=${worker_pid} awake=${awake_kind}:${awake_pid} dashboard=http://127.0.0.1:${mission_control_port}"
