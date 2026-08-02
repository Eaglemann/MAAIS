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

mkdir -p "${log_dir}" "${repository_root}/artifacts/reports" "${repository_root}/backups"

if [[ -f "${current_state}" ]]; then
  existing_worker="$(jq -r '.worker_pid // empty' "${current_state}")"
  if [[ "${existing_worker}" =~ ^[0-9]+$ ]] && kill -0 "${existing_worker}" 2>/dev/null; then
    echo "a paper worker is already running with PID ${existing_worker}" >&2
    exit 1
  fi
fi

cd "${repository_root}"
docker compose up -d --wait postgres
uv run alembic upgrade head

preflight_path="${state_dir}/preflight-$(date -u +%Y%m%dT%H%M%SZ).json"
RUN_MODE=paper_live uv run maais preflight \
  --manifest "${manifest_path}" \
  --restore-verification "${restore_path}" \
  --repository "${repository_root}" \
  --dashboard-dir "${repository_root}/dashboard/dist" \
  > "${preflight_path}"

dashboard_log="${log_dir}/mission-control.log"
worker_log="${log_dir}/paper-worker.log"
awake_log="${log_dir}/sleep-inhibitor.log"
nohup uv run maais mission-control --port "${mission_control_port}" \
  >> "${dashboard_log}" 2>&1 &
dashboard_pid=$!

worker_pid=""
awake_pid=""
cleanup_startup() {
  if [[ "${worker_pid}" =~ ^[0-9]+$ ]]; then
    paper_signal_process_tree "${worker_pid}"
  fi
  if [[ "${awake_pid}" =~ ^[0-9]+$ ]]; then
    kill -TERM "${awake_pid}" 2>/dev/null || true
  fi
  kill -TERM "${dashboard_pid}" 2>/dev/null || true
}
trap cleanup_startup ERR INT TERM

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

nohup env RUN_MODE=paper_live uv run maais paper-live --manifest "${manifest_path}" \
  >> "${worker_log}" 2>&1 &
worker_pid=$!
experiment_id="$(jq -r '.experiment_id' "${manifest_path}")"

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

if ! paper_start_sleep_inhibitor "${worker_pid}" "${awake_log}"; then
  echo "paper worker cannot start without a supported sleep inhibitor" >&2
  cleanup_startup
  exit 1
fi
awake_pid="${PAPER_SLEEP_INHIBITOR_PID}"
awake_kind="${PAPER_SLEEP_INHIBITOR_KIND}"
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
  --arg started_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson worker_pid "${worker_pid}" \
  --argjson dashboard_pid "${dashboard_pid}" \
  --argjson awake_pid "${awake_pid}" \
  --arg awake_kind "${awake_kind}" \
  --argjson port "${mission_control_port}" \
  '{experiment_id:$experiment_id,manifest:$manifest,restore_verification:$restore_verification,preflight:$preflight,started_at:$started_at,worker_pid:$worker_pid,dashboard_pid:$dashboard_pid,awake_pid:$awake_pid,awake_kind:$awake_kind,mission_control_port:$port}' \
  > "${temporary_state}"
mv "${temporary_state}" "${current_state}"

trap - ERR INT TERM
echo "paper week started: experiment=${experiment_id} worker_pid=${worker_pid} awake=${awake_kind}:${awake_pid} dashboard=http://127.0.0.1:${mission_control_port}"
