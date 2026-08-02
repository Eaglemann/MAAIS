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

cd "${repository_root}"
uv run maais verify-ledger
uv run maais daily-report \
  --experiment "${experiment_id}" \
  --date "${report_date}" \
  --output "${repository_root}/artifacts/reports"
uv run maais backup --output "${repository_root}/backups"
"${script_dir}/status-paper-week.sh"
