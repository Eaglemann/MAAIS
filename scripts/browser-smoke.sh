#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repository_root="$(cd "${script_dir}/.." && pwd)"
dashboard_dir="${repository_root}/dashboard"
playwright_cli="${dashboard_dir}/node_modules/.bin/playwright-cli"
base_url="${MAAIS_BROWSER_SMOKE_BASE_URL:-}"
experiment_id="${MAAIS_BROWSER_SMOKE_EXPERIMENT_ID:-}"
config="${MAAIS_BROWSER_SMOKE_CONFIG:-}"
artifact_dir="${MAAIS_BROWSER_SMOKE_ARTIFACT_DIR:-}"
expiry_marker="${MAAIS_BROWSER_SMOKE_EXPIRY_MARKER:-}"
expiry_continue="${MAAIS_BROWSER_SMOKE_EXPIRY_CONTINUE:-}"
session="maais-browser-auth-$$"
current_stage="validate test inputs"

if [[ "${MAAIS_BROWSER_SMOKE_TEST_ONLY:-}" != "1" ]]; then
  echo "browser security smoke is test-only" >&2
  exit 64
fi
if [[ ! "${base_url}" =~ ^https://127\.0\.0\.1:[0-9]+$ ]]; then
  echo "MAAIS_BROWSER_SMOKE_BASE_URL must be a temporary loopback HTTPS origin" >&2
  exit 64
fi
if [[ ! "${experiment_id}" =~ ^[0-9a-f-]{36}$ ]]; then
  echo "MAAIS_BROWSER_SMOKE_EXPERIMENT_ID must be a UUID" >&2
  exit 64
fi
if [[ -z "${MAAIS_BROWSER_SMOKE_PASSPHRASE:-}" ]]; then
  echo "MAAIS_BROWSER_SMOKE_PASSPHRASE must be generated for this test" >&2
  exit 64
fi
for required_path in "${config}" "${expiry_marker%/*}"; do
  if [[ -z "${required_path}" ]]; then
    echo "browser security smoke paths are incomplete" >&2
    exit 64
  fi
done
if [[ -z "${artifact_dir}" || -z "${expiry_marker}" || -z "${expiry_continue}" ]]; then
  echo "browser security smoke paths are incomplete" >&2
  exit 64
fi
if [[ ! -f "${config}" ]]; then
  echo "Playwright test configuration is missing" >&2
  exit 66
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

run_browser_code() {
  local result
  result="$(pw run-code "async (page) => { $1 }" 2>&1)"
  if [[ "${result}" == *"### Error"* ]]; then
    result="${result//${MAAIS_BROWSER_SMOKE_PASSPHRASE}/[REDACTED]}"
    printf '%s\n' "${result}" >&2
    return 1
  fi
}

run_cli_command() {
  local result
  result="$(pw "$@" 2>&1)"
  if [[ "${result}" == *"### Error"* ]]; then
    result="${result//${MAAIS_BROWSER_SMOKE_PASSPHRASE}/[REDACTED]}"
    printf '%s\n' "${result}" >&2
    return 1
  fi
}

cleanup() {
  set +e
  pw close >/dev/null 2>&1
}
report_error() {
  local status="$?"
  echo "browser auth smoke failed during: ${current_stage}" >&2
  exit "${status}"
}
trap cleanup EXIT INT TERM
trap report_error ERR

current_stage="open unauthenticated browser"
cd "${artifact_dir}"
pw open about:blank --config "${config}" >/dev/null

current_stage="deny direct overview before login"
pw goto "${base_url}/api/v1/experiments/${experiment_id}/overview" >/dev/null
overview_denial="$(pw eval '() => document.body.innerText')"
if [[ "${overview_denial}" != *"session_authentication_required"* ]]; then
  echo "direct experiment evidence was not denied before login" >&2
  exit 1
fi

current_stage="deny direct export before login"
pw goto "${base_url}/api/v1/experiments/${experiment_id}/decisions/export.csv" >/dev/null
export_denial="$(pw eval '() => document.body.innerText')"
if [[ "${export_denial}" != *"session_authentication_required"* ]]; then
  echo "direct export was not denied before login" >&2
  exit 1
fi

# Deliberate 401 navigations are recorded as browser console errors. Start the
# authenticated UI phase in a fresh, still-clean context so only application
# errors are evaluated below.
current_stage="open clean authentication browser"
pw close >/dev/null
pw open about:blank --config "${config}" >/dev/null

current_stage="render login boundary"
pw goto "${base_url}/" >/dev/null
pw snapshot >/dev/null
run_browser_code 'await page.getByRole("heading", { name: "Sign in to Mission Control" }).waitFor()'

login() {
  current_stage="enter operator passphrase"
  run_cli_command fill '[aria-label="Operator passphrase"]' "${MAAIS_BROWSER_SMOKE_PASSPHRASE}"
  current_stage="submit operator login"
  run_browser_code 'await page.getByRole("button", { name: "Sign in" }).click(); await page.getByRole("heading", { name: "Mission Control", exact: true }).waitFor({ timeout: 15000 })'
}

login
current_stage="reload authenticated dashboard"
pw snapshot >/dev/null
pw reload >/dev/null
run_browser_code 'await page.getByRole("heading", { name: "Mission Control", exact: true }).waitFor({ timeout: 15000 })'
current_stage="verify live WebSocket"
if ! run_browser_code 'await page.locator(".safety-banner__facts").getByText("live", { exact: true }).waitFor({ timeout: 15000 })'; then
  echo "WebSocket diagnostic requests:" >&2
  pw requests >&2 || true
  echo "WebSocket diagnostic console:" >&2
  pw console info >&2 || true
  run_browser_code 'const observed = []; page.on("websocket", socket => { const item = { url: socket.url(), frames: 0, error: null, closed: false }; observed.push(item); socket.on("framereceived", () => { item.frames += 1; }); socket.on("socketerror", error => { item.error = String(error); }); socket.on("close", () => { item.closed = true; }); }); await page.waitForTimeout(3500); throw new Error(`WebSocket diagnostic: ${JSON.stringify(observed)}`)' || true
  exit 1
fi
current_stage="verify cookie and browser storage"
run_browser_code 'const cookie = (await page.context().cookies()).find(item => item.name === "__Host-maais_session"); if (!cookie || !cookie.httpOnly || !cookie.secure || cookie.sameSite !== "Strict" || cookie.path !== "/") throw new Error("operator cookie flags are incomplete")'
run_browser_code 'const keys = [...Object.keys(await page.evaluate(() => ({ ...localStorage }))), ...Object.keys(await page.evaluate(() => ({ ...sessionStorage })))]; if (keys.some(key => /(auth|csrf|password|token)/i.test(key))) throw new Error("browser storage contains authentication material")'

current_stage="queue authenticated command"
run_browser_code 'await page.getByRole("button", { name: "Pause worker" }).click(); await page.getByLabel("Operator reason").fill("browser security smoke"); await page.getByLabel("Exact confirmation phrase").fill("CONFIRM PAUSE"); await Promise.all([page.waitForResponse(response => response.url().includes("/commands") && response.request().method() === "POST" && response.status() === 202), page.getByRole("button", { name: "Queue confirmed command" }).click()]); await page.getByText("browser security smoke", { exact: true }).waitFor({ timeout: 15000 }); const experimentId = await page.locator(".experiment-picker select").inputValue(); const commandPage = await page.evaluate(async id => (await fetch(`/api/v1/experiments/${id}/commands`)).json(), experimentId); if (commandPage.items?.length !== 1 || commandPage.items[0]?.actor !== "sole_operator") throw new Error("queued command is absent from the authenticated API")'

current_stage="reject tampered CSRF"
run_browser_code 'const experimentId = await page.locator(".experiment-picker select").inputValue(); const origin = await page.evaluate(() => location.origin); const response = await page.request.post(`${origin}/api/v1/experiments/${experimentId}/commands`, { headers: { "Origin": origin, "X-CSRF-Token": "tampered" }, data: { command_type: "pause", idempotency_key: "browser-csrf-tamper", reason: "must be rejected", payload: {}, confirmation: "CONFIRM PAUSE" } }); const session = await page.request.get(`${origin}/api/v1/auth/session`); const sessionBody = await session.json(); if (response.status() !== 403 || session.status() !== 200 || sessionBody.authenticated !== true) throw new Error("tampered CSRF did not fail independently of the session")'

current_stage="advance test session clock"
: > "${expiry_marker}"
clock_advanced=false
for _attempt in $(seq 1 300); do
  if [[ -f "${expiry_continue}" ]]; then
    clock_advanced=true
    break
  fi
  sleep 0.1
done
if [[ "${clock_advanced}" != true ]]; then
  echo "test clock did not advance" >&2
  exit 1
fi
current_stage="render expired session boundary"
run_browser_code 'await page.getByRole("heading", { name: "Sign in to Mission Control" }).waitFor({ timeout: 15000 }); await page.getByText(/session expired/i).waitFor()'

current_stage="classify expiry console evidence"
expiry_console="$(pw console error --json)"
if ! jq -er --arg protected_api "${base_url}/api/v1/" '
  .result as $result
  | [$result | split("\n")[] | select(startswith("[ERROR]"))] as $errors
  | ($result | contains("Warnings: 0"))
    and (
      (($errors | length) == 0 and ($result | contains("Errors: 0")))
      or (
        ($errors | length) > 0
        and all($errors[]; contains("401 (Unauthorized)") and contains($protected_api))
      )
    )
' >/dev/null <<<"${expiry_console}"; then
  redacted_expiry_console="${expiry_console//${MAAIS_BROWSER_SMOKE_PASSPHRASE}/[REDACTED]}"
  printf '%s\n' "${redacted_expiry_console}" >&2
  echo "session expiry produced an unexpected browser console message" >&2
  exit 1
fi
current_stage="clear classified expiry console evidence"
pw console error --clear >/dev/null

login
current_stage="logout authenticated browser"
run_browser_code 'await page.getByRole("button", { name: "Sign out" }).click(); await page.getByRole("heading", { name: "Sign in to Mission Control" }).waitFor({ timeout: 15000 }); await page.getByText(/signed out/i).waitFor()'

current_stage="verify clean recovery console"
console_result="$(pw console error --json)"
if ! jq -er '.result | contains("Errors: 0, Warnings: 0")' \
  >/dev/null <<<"${console_result}"; then
  redacted_console="${console_result//${MAAIS_BROWSER_SMOKE_PASSPHRASE}/[REDACTED]}"
  printf '%s\n' "${redacted_console}" >&2
  echo "real browser recorded a console error or warning" >&2
  exit 1
fi

current_stage="verify post-logout browser history"
pw go-back >/dev/null
back_body="$(pw eval '() => document.body.innerText')"
if [[ "${back_body}" == *"Paper account"* || "${back_body}" == *"Trade Ledger"* ]]; then
  echo "back navigation displayed cached private evidence after logout" >&2
  exit 1
fi
pw goto "${base_url}/" >/dev/null
run_browser_code 'await page.getByRole("heading", { name: "Sign in to Mission Control" }).waitFor({ timeout: 15000 })'

current_stage="deny export after logout"
pw goto "${base_url}/api/v1/experiments/${experiment_id}/decisions/export.csv" >/dev/null
logout_export_denial="$(pw eval '() => document.body.innerText')"
if [[ "${logout_export_denial}" != *"session_authentication_required"* ]]; then
  echo "export was accessible after logout" >&2
  exit 1
fi

echo "browser auth smoke passed: denial, login, reload, WebSocket, CSRF, expiry, logout, back-navigation, and export gates"
