#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
source "${script_dir}/paper-process.sh"
current_state="${repository_root}/artifacts/run-state/current.json"

if [[ ! -f "${current_state}" ]]; then
  echo "no current paper-week state file" >&2
  exit 1
fi

experiment_id="$(jq -r '.experiment_id' "${current_state}")"
worker_pid="$(jq -r '.worker_pid' "${current_state}")"
dashboard_pid="$(jq -r '.dashboard_pid' "${current_state}")"
awake_pid="$(jq -r '.awake_pid // empty' "${current_state}")"
scheduler_pid="$(jq -r '.scheduler_pid // empty' "${current_state}")"
port="$(jq -r '.mission_control_port' "${current_state}")"
docker_context="$(jq -er '.docker_context' "${current_state}")"
postgres_system_identifier="$(jq -er '.postgres_system_identifier' "${current_state}")"
current_postgres_system_identifier="$(
  cd "${repository_root}"
  paper_assert_recorded_postgres_route \
    "${docker_context}" \
    "${postgres_system_identifier}"
)"

worker_alive=false
dashboard_alive=false
awake_alive=false
scheduler_alive=false
kill -0 "${worker_pid}" 2>/dev/null && worker_alive=true
kill -0 "${dashboard_pid}" 2>/dev/null && dashboard_alive=true
[[ "${awake_pid}" =~ ^[0-9]+$ ]] && kill -0 "${awake_pid}" 2>/dev/null && awake_alive=true
[[ "${scheduler_pid}" =~ ^[0-9]+$ ]] && kill -0 "${scheduler_pid}" 2>/dev/null && scheduler_alive=true
api_health="$(curl -fsS "http://127.0.0.1:${port}/api/v1/health" 2>/dev/null || true)"
overview="$(curl -fsS "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview" 2>/dev/null || true)"
database_health="$(cd "${repository_root}" && uv run maais health --experiment "${experiment_id}" --maximum-lag-seconds 180 2>/dev/null || true)"

jq -n \
  --arg experiment_id "${experiment_id}" \
  --arg docker_context "${docker_context}" \
  --arg postgres_system_identifier "${current_postgres_system_identifier}" \
  --argjson worker_alive "${worker_alive}" \
  --argjson dashboard_alive "${dashboard_alive}" \
  --argjson awake_alive "${awake_alive}" \
  --argjson scheduler_alive "${scheduler_alive}" \
  --argjson api_health "${api_health:-null}" \
  --argjson overview "${overview:-null}" \
  --argjson database_health "${database_health:-null}" \
  '{experiment_id:$experiment_id,docker_context:$docker_context,postgres_system_identifier:$postgres_system_identifier,worker_alive:$worker_alive,dashboard_alive:$dashboard_alive,scheduler_alive:$scheduler_alive,awake_alive:$awake_alive,api_health:$api_health,overview:$overview,database_health:$database_health}'

if [[ "${worker_alive}" != true || "${dashboard_alive}" != true || "${scheduler_alive}" != true || "${awake_alive}" != true || -z "${api_health}" || -z "${overview}" || -z "${database_health}" ]]; then
  exit 1
fi
if ! jq -e '.healthy == true' >/dev/null <<<"${database_health}"; then
  exit 1
fi
