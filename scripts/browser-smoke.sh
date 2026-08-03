#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
dashboard_dir="${repository_root}/dashboard"
artifact_dir="${repository_root}/output/playwright/browser-smoke"
playwright_cli="${dashboard_dir}/node_modules/.bin/playwright-cli"
port="${MAAIS_BROWSER_SMOKE_PORT:-4173}"
session="maais-browser-smoke-$$"
server_pid=""

if [[ ! "${port}" =~ ^[0-9]+$ ]] || (( port < 1024 || port > 65535 )); then
  echo "MAAIS_BROWSER_SMOKE_PORT must be an unprivileged TCP port" >&2
  exit 64
fi
if [[ ! -x "${playwright_cli}" ]]; then
  echo "dashboard dependencies are missing; run npm --prefix dashboard ci" >&2
  exit 69
fi

mkdir -p "${artifact_dir}"
export PLAYWRIGHT_CLI_SESSION="${session}"

pw() {
  "${playwright_cli}" "$@"
}

cleanup() {
  set +e
  pw close >/dev/null 2>&1
  if [[ "${server_pid}" =~ ^[0-9]+$ ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill -TERM "${server_pid}" 2>/dev/null
    wait "${server_pid}" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

(
  cd "${dashboard_dir}"
  npm exec -- vite preview --host 127.0.0.1 --port "${port}" --strictPort
) > "${artifact_dir}/vite.log" 2>&1 &
server_pid="$!"

server_ready=false
for _attempt in $(seq 1 30); do
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    break
  fi
  if curl -fsS "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
    server_ready=true
    break
  fi
  sleep 1
done
if [[ "${server_ready}" != true ]]; then
  echo "dashboard preview did not become ready; inspect ${artifact_dir}/vite.log" >&2
  exit 1
fi

cd "${artifact_dir}"
pw open about:blank --browser chromium >/dev/null
pw route '**/api/v1/experiments*' \
  --status 200 \
  --content-type application/json \
  --body '[]' >/dev/null
pw goto "http://127.0.0.1:${port}/" >/dev/null
pw snapshot >/dev/null

empty_state="$(pw find 'No paper experiments yet')"
if [[ "${empty_state}" != *'Found 1 match for "No paper experiments yet"'* ]]; then
  printf '%s\n' "${empty_state}" >&2
  echo "real browser did not render the clean-database Mission Control state" >&2
  exit 1
fi

console_result="$(pw console error --json)"
if ! jq -er '.result | contains("Errors: 0, Warnings: 0")' \
  >/dev/null <<<"${console_result}"; then
  printf '%s\n' "${console_result}" >&2
  echo "real browser recorded an error or warning" >&2
  exit 1
fi

echo "browser smoke passed: http://127.0.0.1:${port}/ rendered the clean paper state with no console errors"
