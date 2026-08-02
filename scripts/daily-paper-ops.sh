#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 EXPERIMENT_ID BERLIN_DATE_YYYY_MM_DD" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
experiment_id="$1"
report_date="$2"
current_state="${repository_root}/artifacts/run-state/current.json"

if [[ ! -f "${current_state}" ]]; then
  echo "no current paper-week state file" >&2
  exit 1
fi
state_experiment_id="$(jq -er '.experiment_id' "${current_state}")"
if [[ "${state_experiment_id}" != "${experiment_id}" ]]; then
  echo "current paper-week state belongs to ${state_experiment_id}, not ${experiment_id}" >&2
  exit 1
fi

cd "${repository_root}"
uv run maais verify-ledger
daily_report_result="$(uv run maais daily-report \
  --experiment "${experiment_id}" \
  --date "${report_date}" \
  --output "${repository_root}/artifacts/reports")"
printf '%s\n' "${daily_report_result}"
report_directory="$(jq -er '.directory' <<<"${daily_report_result}")"
report_id="$(jq -er '.report_id' <<<"${daily_report_result}")"
recorded_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
temporary_state="$(mktemp "${current_state}.tmp.XXXXXX")"
trap 'rm -f "${temporary_state}"' EXIT
jq \
  --arg report_date "${report_date}" \
  --arg report_id "${report_id}" \
  --arg directory "${report_directory}" \
  --arg recorded_at "${recorded_at}" \
  '.daily_reports = ((.daily_reports // []) + [{report_date:$report_date,report_id:$report_id,directory:$directory,recorded_at:$recorded_at}] | unique_by(.report_date))' \
  "${current_state}" > "${temporary_state}"
mv "${temporary_state}" "${current_state}"
trap - EXIT
uv run maais backup --output "${repository_root}/backups"
"${script_dir}/status-paper-week.sh"
