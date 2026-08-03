#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 MANIFEST RESTORE_VERIFICATION QUALIFICATION_BUNDLE [MISSION_CONTROL_PORT]" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
manifest_path="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
state_path="${repository_root}/artifacts/run-state/current.json"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
work_directory="${repository_root}/artifacts/process-drill-work/${timestamp}"
mkdir -p "${work_directory}"
run_started=false

cleanup() {
  exit_code=$?
  trap - EXIT INT TERM
  if [[ "${run_started}" == true && -f "${state_path}" ]]; then
    if jq -e '.run_purpose == "process_drill"' "${state_path}" >/dev/null 2>&1; then
      "${script_dir}/stop-paper-week.sh" \
        > "${work_directory}/cleanup-stop.log" 2>&1 || true
    fi
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

capture_snapshot() {
  target="$1"
  port="$(jq -er '.mission_control_port' "${state_path}")"
  experiment_id="$(jq -er '.experiment_id' "${state_path}")"
  state_copy="${target}.state.json"
  overview_copy="${target}.overview.json"
  ledger_copy="${target}.ledger.json"
  cp "${state_path}" "${state_copy}"
  curl -fsS \
    "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview" \
    > "${overview_copy}"
  (cd "${repository_root}" && uv run maais verify-ledger) > "${ledger_copy}"
  jq -n \
    --arg captured_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --slurpfile state "${state_copy}" \
    --slurpfile overview "${overview_copy}" \
    --slurpfile ledger "${ledger_copy}" \
    '{captured_at:$captured_at,state:$state[0],overview:$overview[0],ledger:$ledger[0]}' \
    > "${target}"
  rm -f "${state_copy}" "${overview_copy}" "${ledger_copy}"
}

start_arguments=("$1" "$2" "$3")
if [[ $# -eq 4 ]]; then
  start_arguments+=("$4")
fi
"${script_dir}/start-paper-drill.sh" "${start_arguments[@]}"
run_started=true

if ! jq -e '.run_purpose == "process_drill"' "${state_path}" >/dev/null; then
  echo "refusing destructive drill because run purpose is not process_drill" >&2
  exit 1
fi

dashboard_baseline="${work_directory}/dashboard-baseline.json"
capture_snapshot "${dashboard_baseline}"
dashboard_pid="$(jq -er '.dashboard_pid' "${state_path}")"
kill -KILL "${dashboard_pid}"
for _attempt in $(seq 1 30); do
  kill -0 "${dashboard_pid}" 2>/dev/null || break
  sleep 1
done
if kill -0 "${dashboard_pid}" 2>/dev/null; then
  echo "dashboard PID survived SIGKILL" >&2
  exit 1
fi

# Keep Mission Control unavailable across the next minute boundary. The worker must
# continue independently and advance its durable checkpoint during this interval.
sleep 70
"${script_dir}/recover-paper-week.sh" dashboard "disposable pre-soak SIGKILL drill" \
  > "${work_directory}/dashboard-recovery.log"
dashboard_recovery_source="$(jq -er '.last_recovery_evidence' "${state_path}")"
cp "${dashboard_recovery_source}" "${work_directory}/dashboard-recovery.json"
capture_snapshot "${work_directory}/dashboard-after.json"

capture_snapshot "${work_directory}/worker-baseline.json"
worker_pid="$(jq -er '.worker_pid' "${state_path}")"
kill -KILL "${worker_pid}"
for _attempt in $(seq 1 30); do
  kill -0 "${worker_pid}" 2>/dev/null || break
  sleep 1
done
if kill -0 "${worker_pid}" 2>/dev/null; then
  echo "worker PID survived SIGKILL" >&2
  exit 1
fi
"${script_dir}/recover-paper-week.sh" worker "disposable pre-soak SIGKILL drill" \
  > "${work_directory}/worker-recovery.log"
worker_recovery_source="$(jq -er '.last_recovery_evidence' "${state_path}")"
cp "${worker_recovery_source}" "${work_directory}/worker-recovery.json"
capture_snapshot "${work_directory}/worker-after.json"

(cd "${repository_root}" && uv run maais process-drill-verdict \
  --manifest "${manifest_path}" \
  --repository "${repository_root}" \
  --dashboard-baseline "${work_directory}/dashboard-baseline.json" \
  --dashboard-recovery "${work_directory}/dashboard-recovery.json" \
  --dashboard-after "${work_directory}/dashboard-after.json" \
  --worker-baseline "${work_directory}/worker-baseline.json" \
  --worker-recovery "${work_directory}/worker-recovery.json" \
  --worker-after "${work_directory}/worker-after.json" \
  --output "${repository_root}/artifacts/process-drills") \
  > "${work_directory}/verdict.json"

"${script_dir}/stop-paper-week.sh" > "${work_directory}/stop.log"
run_started=false
trap - EXIT INT TERM
jq -r '"process drills passed: bundle=" + .directory' "${work_directory}/verdict.json"
