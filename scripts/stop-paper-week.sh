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
echo "paper week stopped cleanly: experiment=${experiment_id} state=${stopped_state}"
