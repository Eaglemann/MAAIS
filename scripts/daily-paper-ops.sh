#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 EXPERIMENT_ID BERLIN_DATE_YYYY_MM_DD" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
source "${script_dir}/paper-process.sh"
experiment_id="$1"
report_date="$2"
state_dir="${repository_root}/artifacts/run-state"
current_state="${state_dir}/current.json"

if [[ ! "${report_date}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "report date must be YYYY-MM-DD" >&2
  exit 64
fi

if [[ ! -f "${current_state}" ]]; then
  echo "no current paper-week state file" >&2
  exit 1
fi
state_experiment_id="$(jq -er '.experiment_id' "${current_state}")"
if [[ "${state_experiment_id}" != "${experiment_id}" ]]; then
  echo "current paper-week state belongs to ${state_experiment_id}, not ${experiment_id}" >&2
  exit 1
fi
docker_context="$(jq -er '.docker_context' "${current_state}")"
postgres_system_identifier="$(jq -er '.postgres_system_identifier' "${current_state}")"

cd "${repository_root}"
paper_assert_recorded_postgres_route \
  "${docker_context}" \
  "${postgres_system_identifier}" >/dev/null
operation_lock="${state_dir}/daily-${experiment_id}-${report_date}.lock"
paper_acquire_operation_lock "${operation_lock}" "daily-paper-close"
temporary_state=""
cleanup() {
  if [[ -n "${temporary_state}" && -f "${temporary_state}" ]]; then
    rm -f "${temporary_state}"
  fi
  paper_release_operation_lock "${operation_lock}"
}
trap cleanup EXIT
uv run maais verify-ledger
daily_report_result="$(uv run maais daily-report \
  --experiment "${experiment_id}" \
  --date "${report_date}" \
  --output "${repository_root}/artifacts/reports" \
  --resume-existing)"
printf '%s\n' "${daily_report_result}"
report_directory="$(jq -er '.directory' <<<"${daily_report_result}")"
report_id="$(jq -er '.report_id' <<<"${daily_report_result}")"
recorded_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
temporary_state="$(mktemp "${current_state}.tmp.XXXXXX")"
jq \
  --arg report_date "${report_date}" \
  --arg report_id "${report_id}" \
  --arg directory "${report_directory}" \
  --arg recorded_at "${recorded_at}" \
  '([(.daily_reports // [])[] | select(.report_date == $report_date)]) as $existing
   | if ($existing | length) > 1 then
       error("multiple run-state reports already exist for " + $report_date)
     elif ($existing | length) == 1 and
       (($existing[0].report_id != $report_id) or ($existing[0].directory != $directory)) then
       error("run-state report identity differs from the verified immutable bundle")
     else
       .daily_reports = ((.daily_reports // []) + [{report_date:$report_date,report_id:$report_id,directory:$directory,recorded_at:$recorded_at}] | unique_by(.report_date))
     end' \
  "${current_state}" > "${temporary_state}"
mv "${temporary_state}" "${current_state}"
temporary_state=""
uv run maais backup --output "${repository_root}/backups"
"${script_dir}/status-paper-week.sh"
