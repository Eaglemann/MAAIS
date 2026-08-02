#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
current_state="${repository_root}/artifacts/run-state/current.json"

if [[ ! -f "${current_state}" ]]; then
  echo "no current paper-week state file" >&2
  exit 1
fi

experiment_id="$(jq -r '.experiment_id' "${current_state}")"
worker_pid="$(jq -r '.worker_pid' "${current_state}")"
dashboard_pid="$(jq -r '.dashboard_pid' "${current_state}")"
port="$(jq -r '.mission_control_port' "${current_state}")"

worker_alive=false
dashboard_alive=false
kill -0 "${worker_pid}" 2>/dev/null && worker_alive=true
kill -0 "${dashboard_pid}" 2>/dev/null && dashboard_alive=true
api_health="$(curl -fsS "http://127.0.0.1:${port}/api/v1/health" 2>/dev/null || true)"
overview="$(curl -fsS "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview" 2>/dev/null || true)"
database_health="$(cd "${repository_root}" && uv run maais health --experiment "${experiment_id}" --maximum-lag-seconds 180 2>/dev/null || true)"

jq -n \
  --arg experiment_id "${experiment_id}" \
  --argjson worker_alive "${worker_alive}" \
  --argjson dashboard_alive "${dashboard_alive}" \
  --argjson api_health "${api_health:-null}" \
  --argjson overview "${overview:-null}" \
  --argjson database_health "${database_health:-null}" \
  '{experiment_id:$experiment_id,worker_alive:$worker_alive,dashboard_alive:$dashboard_alive,api_health:$api_health,overview:$overview,database_health:$database_health}'

if [[ "${worker_alive}" != true || "${dashboard_alive}" != true || -z "${api_health}" || -z "${overview}" || -z "${database_health}" ]]; then
  exit 1
fi
if ! jq -e '.healthy == true' >/dev/null <<<"${database_health}"; then
  exit 1
fi
