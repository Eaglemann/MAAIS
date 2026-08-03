#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
source "${script_dir}/paper-process.sh"
state_dir="${repository_root}/artifacts/run-state"
current_state="${state_dir}/current.json"

if [[ ! -f "${current_state}" ]]; then
  echo "no current paper-week state file" >&2
  exit 1
fi

experiment_id="$(jq -r '.experiment_id' "${current_state}")"
worker_pid="$(jq -r '.worker_pid' "${current_state}")"
dashboard_pid="$(jq -r '.dashboard_pid' "${current_state}")"
awake_pid="$(jq -r '.awake_pid // empty' "${current_state}")"
scheduler_pid="$(jq -r '.scheduler_pid // empty' "${current_state}")"
scheduler_session="$(jq -r '.scheduler_session // empty' "${current_state}")"
mission_control_port="$(jq -er '.mission_control_port' "${current_state}")"
control_token_file="$(jq -er '.control_token_file' "${current_state}")"
stop_request_body="${state_dir}/.stop-command-${experiment_id%%-*}.json"
stop_request_config="${state_dir}/.stop-command-${experiment_id%%-*}.curl"
cleanup_stop_request() {
  rm -f "${stop_request_body}" "${stop_request_config}"
}
trap cleanup_stop_request EXIT INT TERM

if [[ "${scheduler_pid}" =~ ^[0-9]+$ ]] && kill -0 "${scheduler_pid}" 2>/dev/null; then
  paper_signal_process_tree "${scheduler_pid}"
  for _attempt in $(seq 1 30); do
    kill -0 "${scheduler_pid}" 2>/dev/null || break
    sleep 1
  done
fi
if [[ "${scheduler_pid}" =~ ^[0-9]+$ ]] && kill -0 "${scheduler_pid}" 2>/dev/null; then
  echo "daily-close supervisor did not stop; refusing to race the final close" >&2
  exit 1
fi
if [[ -n "${scheduler_session}" ]]; then
  paper_stop_tmux_session "${scheduler_session}"
fi

if kill -0 "${worker_pid}" 2>/dev/null; then
  if ! kill -0 "${dashboard_pid}" 2>/dev/null \
    || ! paper_curl -fsS "http://127.0.0.1:${mission_control_port}/api/v1/health" >/dev/null 2>&1; then
    echo "Mission Control must be healthy before an audited STOP; recover it first" >&2
    exit 1
  fi
  stop_idempotency_key="paper-week-stop-${experiment_id}"
  jq -n \
    --arg idempotency_key "${stop_idempotency_key}" \
    '{"command_type":"stop","idempotency_key":$idempotency_key,"reason":"stop the local paper week cleanly","payload":{"source":"stop-paper-week.sh"},"confirmation":"CONFIRM STOP"}' \
    > "${stop_request_body}"
  control_token="$(<"${control_token_file}")"
  umask 077
  printf '%s\n' \
    "url = \"http://127.0.0.1:${mission_control_port}/api/v1/experiments/${experiment_id}/commands\"" \
    'request = "POST"' \
    'header = "Content-Type: application/json"' \
    "header = \"Authorization: Bearer ${control_token}\"" \
    "data = \"@${stop_request_body}\"" \
    > "${stop_request_config}"
  unset control_token
  stop_response="$(paper_curl --silent --show-error --fail --config "${stop_request_config}")"
  cleanup_stop_request
  stop_command_id="$(jq -er '.command_id' <<<"${stop_response}")"
  command_status=""
  command_response=""
  for _attempt in $(seq 1 60); do
    command_response="$(paper_curl -fsS "http://127.0.0.1:${mission_control_port}/api/v1/commands/${stop_command_id}" 2>/dev/null || true)"
    if [[ -n "${command_response}" ]]; then
      command_status="$(jq -r '.status // empty' <<<"${command_response}")"
    fi
    if [[ "${command_status}" == "rejected" ]]; then
      echo "audited STOP was rejected; flatten open exposure first: ${command_response}" >&2
      exit 1
    fi
    if [[ "${command_status}" == "completed" ]]; then
      break
    fi
    sleep 1
  done
  if [[ "${command_status}" != "completed" ]]; then
    echo "paper worker did not complete the audited STOP command" >&2
    exit 1
  fi

  for _attempt in $(seq 1 60); do
    kill -0 "${worker_pid}" 2>/dev/null || break
    sleep 1
  done
fi
if kill -0 "${worker_pid}" 2>/dev/null; then
  paper_signal_process_tree "${worker_pid}"
  for _attempt in $(seq 1 60); do
    kill -0 "${worker_pid}" 2>/dev/null || break
    sleep 1
  done
fi
if kill -0 "${worker_pid}" 2>/dev/null; then
  echo "worker did not stop gracefully; it was not force-killed. Inspect logs and recover manually." >&2
  exit 1
fi

if [[ "${awake_pid}" =~ ^[0-9]+$ ]] && kill -0 "${awake_pid}" 2>/dev/null; then
  kill -TERM "${awake_pid}" 2>/dev/null || true
fi

if kill -0 "${dashboard_pid}" 2>/dev/null; then
  kill -TERM "${dashboard_pid}"
fi

cd "${repository_root}"
uv run maais verify-ledger
report_date="$(TZ=Europe/Berlin date +%F)"
uv run maais daily-report \
  --experiment "${experiment_id}" \
  --date "${report_date}" \
  --output "${repository_root}/artifacts/reports" \
  --allow-partial
uv run maais backup --output "${repository_root}/backups"

stopped_state="${state_dir}/stopped-$(date -u +%Y%m%dT%H%M%SZ).json"
jq --arg stopped_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '. + {stopped_at:$stopped_at}' \
  "${current_state}" > "${stopped_state}"
mv "${current_state}" "${current_state}.previous"
trap - EXIT INT TERM
echo "paper week stopped cleanly: experiment=${experiment_id} state=${stopped_state}"
