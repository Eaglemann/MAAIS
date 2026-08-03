#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 dashboard|worker|scheduler REASON" >&2
  exit 64
fi

service="$1"
reason="$2"
if [[ "${service}" != "dashboard" && "${service}" != "worker" && "${service}" != "scheduler" ]]; then
  echo "service must be dashboard, worker, or scheduler" >&2
  exit 64
fi
if [[ -z "${reason//[[:space:]]/}" ]]; then
  echo "recovery reason is required" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
source "${script_dir}/paper-process.sh"
state_dir="${repository_root}/artifacts/run-state"
current_state="${state_dir}/current.json"
evidence_dir="${state_dir}/recovery-evidence"

if [[ ! -f "${current_state}" ]]; then
  echo "no current paper-week state file" >&2
  exit 1
fi

experiment_id="$(jq -r '.experiment_id' "${current_state}")"
manifest_path="$(jq -r '.manifest' "${current_state}")"
port="$(jq -r '.mission_control_port' "${current_state}")"
control_token_file="$(jq -er '.control_token_file' "${current_state}")"
worker_pid="$(jq -r '.worker_pid' "${current_state}")"
dashboard_pid="$(jq -r '.dashboard_pid' "${current_state}")"
awake_pid="$(jq -r '.awake_pid // empty' "${current_state}")"
scheduler_pid="$(jq -r '.scheduler_pid // empty' "${current_state}")"
worker_session="$(jq -r '.worker_session' "${current_state}")"
dashboard_session="$(jq -r '.dashboard_session' "${current_state}")"
awake_session="$(jq -r '.awake_session' "${current_state}")"
scheduler_session="$(jq -r '.scheduler_session // empty' "${current_state}")"
docker_context="$(jq -er '.docker_context' "${current_state}")"
postgres_system_identifier="$(jq -er '.postgres_system_identifier' "${current_state}")"
target_pid="${dashboard_pid}"
if [[ "${service}" == "worker" ]]; then
  target_pid="${worker_pid}"
elif [[ "${service}" == "scheduler" ]]; then
  target_pid="${scheduler_pid}"
fi
if [[ ! "${target_pid}" =~ ^[0-9]+$ ]]; then
  echo "recorded ${service} PID is invalid" >&2
  exit 1
fi
if kill -0 "${target_pid}" 2>/dev/null; then
  echo "refusing recovery while recorded ${service} PID ${target_pid} is alive" >&2
  exit 1
fi
if [[ "${service}" == "scheduler" ]]; then
  if ! kill -0 "${worker_pid}" 2>/dev/null \
    || ! kill -0 "${dashboard_pid}" 2>/dev/null \
    || ! kill -0 "${awake_pid}" 2>/dev/null \
    || ! curl -fsS "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1; then
    echo "worker, Mission Control, and sleep inhibitor must be healthy before scheduler recovery" >&2
    exit 1
  fi
fi

mkdir -p "${evidence_dir}"
cd "${repository_root}"
paper_ensure_control_token "${control_token_file}"
paper_assert_recorded_postgres_route \
  "${docker_context}" \
  "${postgres_system_identifier}" >/dev/null
before_ledger="$(uv run maais verify-ledger)"
before_overview="$(curl -fsS "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview" 2>/dev/null || true)"
recovery_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
new_worker_pid="${worker_pid}"
new_dashboard_pid="${dashboard_pid}"
new_awake_pid="${awake_pid:-0}"
new_scheduler_pid="${scheduler_pid:-0}"

if [[ "${service}" == "dashboard" ]]; then
  paper_stop_tmux_session "${dashboard_session}"
  dashboard_log="${state_dir}/logs/mission-control-${experiment_id%%-*}.log"
  printf -v dashboard_command \
    'cd %q && exec env RUN_MODE=paper_live ENVIRONMENT=production MISSION_CONTROL_TOKEN_FILE=%q uv run maais mission-control --port %q >> %q 2>&1' \
    "${repository_root}" "${control_token_file}" "${port}" "${dashboard_log}"
  paper_start_tmux_session "${dashboard_session}" "${dashboard_command}"
  new_dashboard_pid="${PAPER_TMUX_PANE_PID}"
  dashboard_ready=false
  for _attempt in $(seq 1 30); do
    if ! kill -0 "${new_dashboard_pid}" 2>/dev/null; then
      break
    fi
    if curl -fsS "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1; then
      dashboard_ready=true
      break
    fi
    sleep 1
  done
  if [[ "${dashboard_ready}" != true ]]; then
    paper_stop_tmux_session "${dashboard_session}"
    echo "Mission Control recovery failed; inspect ${dashboard_log}" >&2
    exit 1
  fi
elif [[ "${service}" == "worker" ]]; then
  if ! kill -0 "${dashboard_pid}" 2>/dev/null \
    || ! curl -fsS "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1; then
    echo "Mission Control must be healthy before worker recovery" >&2
    exit 1
  fi
  if [[ -z "${before_overview}" ]]; then
    echo "worker recovery requires a readable pre-recovery overview" >&2
    exit 1
  fi
  before_epoch="$(jq -r '.runtime.lease_epoch // 0' <<<"${before_overview}")"
  lease_expires_at="$(jq -r '.runtime.lease_expires_at // empty' <<<"${before_overview}")"
  paper_stop_tmux_session "${worker_session}"
  if [[ "${awake_pid}" =~ ^[0-9]+$ ]] && kill -0 "${awake_pid}" 2>/dev/null; then
    kill -TERM "${awake_pid}" 2>/dev/null || true
  fi
  paper_stop_tmux_session "${awake_session}"

  lease_expired=false
  for _attempt in $(seq 1 45); do
    if [[ -z "${lease_expires_at}" ]] || uv run python -c \
      'from datetime import datetime, timezone; import sys; value=datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")); raise SystemExit(0 if value <= datetime.now(timezone.utc) else 1)' \
      "${lease_expires_at}"; then
      lease_expired=true
      break
    fi
    sleep 1
  done
  if [[ "${lease_expired}" != true ]]; then
    echo "recorded worker lease did not expire within 45 seconds" >&2
    exit 1
  fi

  worker_log="${state_dir}/logs/paper-worker-${experiment_id%%-*}.log"
  printf -v worker_command \
    'cd %q && exec env RUN_MODE=paper_live ENVIRONMENT=production uv run maais paper-live --manifest %q >> %q 2>&1' \
    "${repository_root}" "${manifest_path}" "${worker_log}"
  paper_start_tmux_session "${worker_session}" "${worker_command}"
  new_worker_pid="${PAPER_TMUX_PANE_PID}"
  worker_ready=false
  for _attempt in $(seq 1 90); do
    if ! kill -0 "${new_worker_pid}" 2>/dev/null; then
      break
    fi
    overview="$(curl -fsS "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview" 2>/dev/null || true)"
    if [[ -n "${overview}" ]] && jq -e --argjson before_epoch "${before_epoch}" \
      '.runtime.worker_status == "running" and .runtime.lease_status == "active" and .runtime.lease_epoch > $before_epoch' \
      >/dev/null <<<"${overview}"; then
      worker_ready=true
      break
    fi
    sleep 1
  done
  if [[ "${worker_ready}" != true ]]; then
    paper_stop_tmux_session "${worker_session}"
    echo "paper worker recovery failed; inspect ${worker_log}" >&2
    exit 1
  fi

  awake_kind="$(paper_sleep_inhibitor_kind)"
  awake_log="${state_dir}/logs/sleep-inhibitor-${experiment_id%%-*}.log"
  printf -v awake_command \
    'exec bash %q hold-awake %q >> %q 2>&1' \
    "${script_dir}/paper-process.sh" "${new_worker_pid}" "${awake_log}"
  paper_start_tmux_session "${awake_session}" "${awake_command}"
  new_awake_pid="${PAPER_TMUX_PANE_PID}"
  sleep 1
  if ! kill -0 "${new_awake_pid}" 2>/dev/null; then
    paper_signal_process_tree "${new_worker_pid}"
    paper_stop_tmux_session "${worker_session}"
    paper_stop_tmux_session "${awake_session}"
    echo "sleep inhibitor recovery failed" >&2
    exit 1
  fi
fi

if [[ ! "${new_scheduler_pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${new_scheduler_pid}" 2>/dev/null; then
  if [[ -z "${scheduler_session}" ]]; then
    scheduler_session="maais-daily-${experiment_id%%-*}"
  fi
  paper_stop_tmux_session "${scheduler_session}"
  scheduler_log="${state_dir}/logs/daily-supervisor-${experiment_id%%-*}.log"
  printf -v scheduler_command \
    'cd %q && while ! jq -e --argjson pid "$$" '\''.scheduler_pid == $pid'\'' %q >/dev/null 2>&1; do sleep 0.1; done; exec env RUN_MODE=paper_live ENVIRONMENT=production uv run maais daily-supervisor --state %q --close-script %q >> %q 2>&1' \
    "${repository_root}" "${current_state}" "${current_state}" "${script_dir}/daily-paper-ops.sh" "${scheduler_log}"
  paper_start_tmux_session "${scheduler_session}" "${scheduler_command}"
  new_scheduler_pid="${PAPER_TMUX_PANE_PID}"
  sleep 1
  if ! kill -0 "${new_scheduler_pid}" 2>/dev/null; then
    paper_stop_tmux_session "${scheduler_session}"
    echo "daily-close supervisor recovery failed; inspect ${scheduler_log}" >&2
    exit 1
  fi
fi

after_overview="$(curl -fsS "http://127.0.0.1:${port}/api/v1/experiments/${experiment_id}/overview")"
after_ledger="$(uv run maais verify-ledger)"
recovered_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
evidence_target="${evidence_dir}/${timestamp}-${service}.json"
evidence_temporary="${evidence_target}.tmp"
state_temporary="${current_state}.recovery.tmp"

jq -n \
  --arg service "${service}" \
  --arg reason "${reason}" \
  --arg experiment_id "${experiment_id}" \
  --arg manifest "${manifest_path}" \
  --arg recovery_started_at "${recovery_started_at}" \
  --arg recovered_at "${recovered_at}" \
  --argjson prior_pid "${target_pid}" \
  --argjson worker_pid "${new_worker_pid}" \
  --argjson dashboard_pid "${new_dashboard_pid}" \
  --argjson awake_pid "${new_awake_pid}" \
  --argjson scheduler_pid "${new_scheduler_pid}" \
  --argjson before_overview "${before_overview:-null}" \
  --argjson after_overview "${after_overview}" \
  --argjson before_ledger "${before_ledger}" \
  --argjson after_ledger "${after_ledger}" \
  '{service:$service,reason:$reason,experiment_id:$experiment_id,manifest:$manifest,recovery_started_at:$recovery_started_at,recovered_at:$recovered_at,prior_pid:$prior_pid,current_pids:{worker:$worker_pid,dashboard:$dashboard_pid,scheduler:$scheduler_pid,awake:$awake_pid},before:{overview:$before_overview,ledger:$before_ledger},after:{overview:$after_overview,ledger:$after_ledger}}' \
  > "${evidence_temporary}"

jq \
  --argjson worker_pid "${new_worker_pid}" \
  --argjson dashboard_pid "${new_dashboard_pid}" \
  --argjson awake_pid "${new_awake_pid}" \
  --argjson scheduler_pid "${new_scheduler_pid}" \
  --arg scheduler_session "${scheduler_session}" \
  --arg recovered_at "${recovered_at}" \
  --arg evidence "${evidence_target}" \
  '.worker_pid=$worker_pid | .dashboard_pid=$dashboard_pid | .scheduler_pid=$scheduler_pid | .scheduler_session=$scheduler_session | .awake_pid=$awake_pid | .last_recovery_at=$recovered_at | .last_recovery_evidence=$evidence' \
  "${current_state}" > "${state_temporary}"
mv "${evidence_temporary}" "${evidence_target}"
mv "${state_temporary}" "${current_state}"

echo "paper ${service} recovered: experiment=${experiment_id} evidence=${evidence_target}"
