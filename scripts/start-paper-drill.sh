#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: $0 MANIFEST RESTORE_VERIFICATION QUALIFICATION_BUNDLE [MISSION_CONTROL_PORT]" >&2
  exit 64
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env MAAIS_RUN_PURPOSE=process_drill \
  "${script_dir}/start-paper-week.sh" "$@"
