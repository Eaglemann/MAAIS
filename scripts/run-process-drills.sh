#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 MANIFEST RESTORE_VERIFICATION QUALIFICATION_BUNDLE [MISSION_CONTROL_PORT]" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
source "${script_dir}/paper-process.sh"
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
  paper_curl -fsS \
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

wait_for_post_recovery_cycle() {
  local recovery="$1"
  local port
  local experiment_id
  local baseline_decisions
  local expected_symbols
  local minimum_decisions
  local recovered_at
  local overview
  local current_decisions
  local stable_decisions=""
  local stable_observations=0
  local post_recovery_clean

  port="$(jq -er '.mission_control_port' "${state_path}")"
  experiment_id="$(jq -er '.experiment_id' "${state_path}")"
  baseline_decisions="$(jq -er '.before.overview.decisions.total' "${recovery}")"
  expected_symbols="$(jq -er '.symbols | length' "${manifest_path}")"
  minimum_decisions="$((baseline_decisions + expected_symbols))"
  recovered_at="$(jq -er '.recovered_at' "${recovery}")"

  for _attempt in $(seq 1 120); do
    overview="$(paper_curl -fsS \
      "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview" \
      2>/dev/null || true)"
    if [[ -n "${overview}" ]] && jq -e \
      --argjson minimum_decisions "${minimum_decisions}" \
      --arg recovered_at "${recovered_at}" \
      '.decisions.total >= $minimum_decisions and (.freshness.latest_cursor_update_at // "") > $recovered_at and .freshness.active_recoveries == 0' \
      >/dev/null <<<"${overview}"; then
      current_decisions="$(jq -er '.decisions.total' <<<"${overview}")"
      if [[ "${current_decisions}" == "${stable_decisions}" ]]; then
        stable_observations="$((stable_observations + 1))"
      else
        stable_decisions="${current_decisions}"
        stable_observations=1
      fi
      if ((stable_observations >= 3)); then
        post_recovery_clean="$(jq -r \
          '.operations.open_incidents == 0 and .operations.review_incidents == 0' \
          <<<"${overview}")"
        echo "post-recovery cycle observed: decisions=${current_decisions} clean=${post_recovery_clean}" >&2
        return 0
      fi
    else
      stable_decisions=""
      stable_observations=0
    fi
    sleep 1
  done
  echo "replacement worker did not complete a full post-recovery symbol cycle" >&2
  return 1
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
paper_wait_for_minute_window 10 15
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
wait_for_post_recovery_cycle "${work_directory}/worker-recovery.json"
capture_snapshot "${work_directory}/worker-after.json"

experiment_id="$(jq -er '.experiment_id' "${state_path}")"
drill_report_date="$(
  cd "${repository_root}"
  uv run python -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; print((datetime.now(ZoneInfo("Europe/Berlin")).date() - timedelta(days=1)).isoformat())'
)"
daily_close_raw="${work_directory}/daily-close.raw.jsonl"
daily_close_stderr="${work_directory}/daily-close.stderr.log"
docker() {
  echo "Docker is forbidden during the process-drill daily close" >&2
  return 97
}
export -f docker
if ! ENVIRONMENT=production RUN_MODE=paper_live \
  "${script_dir}/daily-paper-ops.sh" "${experiment_id}" "${drill_report_date}" \
  > "${daily_close_raw}" 2> "${daily_close_stderr}"; then
  unset -f docker
  cat "${daily_close_stderr}" >&2
  exit 1
fi
unset -f docker
backup_manifest_path="$(jq -ers '.[2].manifest' "${daily_close_raw}")"
if [[ ! -f "${backup_manifest_path}" || -L "${backup_manifest_path}" ]]; then
  echo "daily close did not produce a regular backup manifest" >&2
  exit 1
fi
jq -s \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg experiment_id "${experiment_id}" \
  --arg report_date "${drill_report_date}" \
  --slurpfile state "${state_path}" \
  --slurpfile backup_manifest "${backup_manifest_path}" \
  'if length != 4 then
     error("daily close must emit ledger, report, backup, and status evidence")
   else
     {
       docker_disabled:true,
       completed_at:$completed_at,
       experiment_id:$experiment_id,
       report_date:$report_date,
       ledger:.[0],
       report:.[1],
       backup:.[2],
       backup_manifest:$backup_manifest[0],
       status:.[3],
       run_state_report:([($state[0].daily_reports // [])[] | select(.report_date == $report_date)] | if length == 1 then .[0] else null end)
     }
   end' \
  "${daily_close_raw}" > "${work_directory}/daily-close.json"

(cd "${repository_root}" && uv run maais process-drill-verdict \
  --manifest "${manifest_path}" \
  --repository "${repository_root}" \
  --dashboard-baseline "${work_directory}/dashboard-baseline.json" \
  --dashboard-recovery "${work_directory}/dashboard-recovery.json" \
  --dashboard-after "${work_directory}/dashboard-after.json" \
  --worker-baseline "${work_directory}/worker-baseline.json" \
  --worker-recovery "${work_directory}/worker-recovery.json" \
  --worker-after "${work_directory}/worker-after.json" \
  --daily-close "${work_directory}/daily-close.json" \
  --output "${repository_root}/artifacts/process-drills") \
  > "${work_directory}/verdict.json"

"${script_dir}/stop-paper-week.sh" > "${work_directory}/stop.log"
run_started=false
trap - EXIT INT TERM
jq -r '"process drills passed: bundle=" + .directory' "${work_directory}/verdict.json"
