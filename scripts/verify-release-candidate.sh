#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'release candidate verification failed: %s\n' "$1" >&2
  exit 1
}

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repository_root="$(CDPATH= cd -- "${script_directory}/.." && pwd -P)"
expected_sha="${1:-${GITHUB_SHA:-}}"

if [[ ! "${expected_sha}" =~ ^[0-9a-f]{40}$ ]]; then
  fail "expected commit must be one lowercase 40-character Git SHA"
fi

cd "${repository_root}"
actual_sha="$(git rev-parse HEAD)"
if [[ "${actual_sha}" != "${expected_sha}" ]]; then
  fail "checked-out commit differs from the expected commit"
fi
if [[ -n "${GITHUB_SHA:-}" && "${GITHUB_SHA}" != "${expected_sha}" ]]; then
  fail "workflow commit differs from the expected commit"
fi
if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  fail "worktree contains tracked or untracked drift"
fi
git diff --check

descriptor_inputs=(
  Dockerfile
  uv.lock
  dashboard/package-lock.json
  dashboard/dist/asset-manifest.json
  .github/workflows/ci.yml
)
for path in "${descriptor_inputs[@]}"; do
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    fail "candidate input is missing, non-regular, or symbolic: ${path}"
  fi
done
git ls-files --error-unmatch Dockerfile uv.lock dashboard/package-lock.json >/dev/null
uv lock --check

schema_heads="$(uv run alembic heads)"
if [[ "${schema_heads}" != "0022 (head)" ]]; then
  fail "schema head is not exactly 0022"
fi
python3 scripts/verify_dashboard_assets.py dashboard/dist \
  --expected-release "${expected_sha}" >/dev/null

required_jobs=(
  quality
  test
  frontend
  frontend-sentry-release
  security
  postgres-integration
  artifact-contract
  redaction-contract
  container-contract
  release-candidate
)
for job in "${required_jobs[@]}"; do
  if ! grep -Eq "^  ${job}:$" .github/workflows/ci.yml; then
    fail "required CI job is missing: ${job}"
  fi
done

printf '{"candidate_sha":"%s","outcome":"passed","schema_revision":"0022"}\n' \
  "${actual_sha}"
