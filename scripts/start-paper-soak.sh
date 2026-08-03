#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 MANIFEST RESTORE_VERIFICATION QUALIFICATION_BUNDLE PROCESS_DRILL_BUNDLE [MISSION_CONTROL_PORT]" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
process_drill_bundle="$(cd "$4" && pwd)"
arguments=("$1" "$2" "$3")
if [[ $# -eq 5 ]]; then
  arguments+=("$5")
fi
exec env \
  MAAIS_RUN_PURPOSE=soak \
  MAAIS_PROCESS_DRILL_BUNDLE="${process_drill_bundle}" \
  "${script_dir}/start-paper-week.sh" "${arguments[@]}"
