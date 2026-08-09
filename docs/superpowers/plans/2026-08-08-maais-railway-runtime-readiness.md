# MAAIS Railway Runtime and Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package one immutable non-root candidate for the three Railway service roles, preserve every local evidence gate in cloud form, prove recovery behavior in qualification, and run a separately authorized uninterrupted 24-hour Railway soak before stopping for seven-day authorization.

**Architecture:** A multi-stage digest-pinned Docker build creates the dashboard once, installs locked Python dependencies, generates a canonical candidate descriptor, and emits a minimal runtime image. Explicit web, worker, operations, verifier, and migrator commands share the image but enforce role/database authority. Cloud-specific preflight, process-drill, and soak-verdict evaluators extend existing gate names and produce dual-store immutable evidence.

**Tech Stack:** Dockerfile, Python 3.12, uv, Node 22, npm, FastAPI/Uvicorn, Railway, PostgreSQL 16, Sentry, S3-compatible storage, GitHub Actions, shell runbooks, pytest, Vitest, Playwright.

## Global Constraints

- Do not invoke Docker or any credential helper merely to inspect status. Container build execution requires the user's approval if it could access Docker Desktop/Keychain; static container tests and credential-free builders are preferred.
- One image digest and candidate descriptor serve every role. A role-specific code build is invalid.
- The final image contains no `.git`, `.env`, tests, source maps, Sentry upload token, cloud credentials, exchange credentials, cache, package-manager metadata, or compiler toolchain.
- The final process runs as a fixed non-root UID/GID on a read-only root filesystem except a bounded `/tmp` working area.
- Worker and operations have no public domain. PostgreSQL and object storage remain private. Mission Control is the only public service.
- Migrations run once under `migrator` before readiness and never from worker startup or during a timed run.
- Railway application services use one replica in the selected European region, app sleep off, autodeploy off, and restart policy `NEVER` for official qualification/soak.
- Every cloud gate emits stable names and direct evidence. No generic `healthy` boolean may replace existing preflight/process-drill/soak gates.
- A cloud drill may intentionally replace services only in qualification with `run_purpose=process_drill`. Production soak forbids replacement/recovery.
- Railway spend alerts are soft. A hard cutoff capable of terminating a timed run is a failed preflight gate.
- No 24-hour soak start without explicit authorization after production preflight. No seven-day run start under this plan.

---

## Runtime Commands Produced

The final `maais` CLI must expose:

```text
maais cloud-web
maais cloud-worker
maais cloud-operations
maais cloud-verifier --run-id UUID
maais cloud-migrate --expected-revision 0022
maais cloud-preflight --run-id UUID --output DIRECTORY
maais cloud-process-drill-verdict --run-id UUID --output DIRECTORY
maais cloud-soak-verdict --run-id UUID --output DIRECTORY
```

Worker and operations read the frozen `MAAIS_RUN_ID`; worker also reads `MAAIS_MANIFEST_ARTIFACT_ID`. The manifest setting is an artifact catalog record UUID, not an arbitrary local path or mutable object key. Local commands and scripts keep their existing interfaces.

## Task 1: Add Role Entrypoints and Lifecycle Boundaries

**Files:**

- Create: `maais/platform/services.py`
- Create: `maais/platform/lifecycle.py`
- Modify: `maais/cli.py`
- Modify: `maais/api/app.py`
- Test: `tests/unit/platform/test_service_entrypoints.py`
- Test: `tests/unit/platform/test_service_lifecycle.py`

**Consumes:** Runtime identity verification, role settings, Mission Control app, worker supervisor, health supervisor, artifact-backed manifest.

**Produces:** Strict role entrypoints with readiness state, heartbeat, graceful stop, and terminal non-zero boundaries.

- [x] Write failing tests proving each command rejects every other configured role, verifies identity/schema/database role before work, registers one boot, heartbeats monotonically, marks clean stop, and leaves terminal failure unmasked.

  ```python
  @pytest.mark.parametrize(
      ("command", "required_role"),
      (
          ("cloud-web", ServiceRole.WEB),
          ("cloud-worker", ServiceRole.WORKER),
          ("cloud-operations", ServiceRole.OPERATIONS),
          ("cloud-verifier", ServiceRole.VERIFIER),
          ("cloud-migrate", ServiceRole.MIGRATOR),
      ),
  )
  def test_cloud_command_rejects_wrong_role(command: str, required_role: ServiceRole) -> None:
      with pytest.raises(ServiceRoleMismatch):
          invoke_cloud_command(command, configured_role=ServiceRole.WEB if required_role is not ServiceRole.WEB else ServiceRole.WORKER)
  ```

- [x] Write worker tests proving manifest retrieval verifies artifact/catalog/content/candidate hash before experiment creation, exchange credentials remain absent, the worker remains `standby` until it consumes the persisted audited start command, and only the worker connection can create trading state.

- [x] Run tests and confirm entrypoints are absent.

  ```bash
  uv run pytest -q tests/unit/platform/test_service_entrypoints.py tests/unit/platform/test_service_lifecycle.py
  ```

- [x] Implement a lifecycle context manager that generates a boot ID, binds telemetry, verifies/registers identity, starts a heartbeat task, flips readiness only after role startup checks, and records stop in `finally`.

  ```python
  @asynccontextmanager
  async def cloud_service_lifecycle(
      *,
      role: ServiceRole,
      run_id: UUID | None,
      settings: Settings,
      clock: Clock,
  ) -> AsyncIterator[ServiceLifecycle]:
      raise NotImplementedError
  ```

- [x] Use Uvicorn's programmatic server for `cloud-web`, bind `host="::"` and integer `PORT`, disable proxy trust except Railway's documented boundary, and drive liveness/readiness from lifecycle state.

- [x] Ensure SIGTERM allows bounded shutdown, releases worker lease through existing logic, records stop, flushes structured logs/Sentry, and exits before Railway's termination deadline. A clean Railway process start never activates a standby run by itself.

- [x] Run entrypoint tests plus local CLI/worker regressions.

  ```bash
  uv run pytest -q tests/unit/platform/test_service_entrypoints.py tests/unit/platform/test_service_lifecycle.py tests/unit/orchestration tests/unit/api
  uv run ruff check maais/platform maais/cli.py maais/api/app.py tests/unit/platform
  uv run pyright maais/platform maais/cli.py maais/api/app.py
  ```

  Expected: all commands exit `0`.

- [x] Commit.

  ```bash
  git add maais/platform/services.py maais/platform/lifecycle.py maais/cli.py maais/api/app.py tests/unit/platform/test_service_entrypoints.py tests/unit/platform/test_service_lifecycle.py
  git commit -m "feat: add cloud service entrypoints"
  git push origin feat/paper-platform-baseline
  ```

## Task 2: Add the Deterministic Multi-Stage Image

**Files:**

- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `scripts/resolve-base-image-digests.py`
- Create: `tests/container/test_dockerfile_contract.py`
- Create: `tests/container/test_runtime_image.py`

**Consumes:** Locked dependencies, dashboard source, Git SHA build argument, candidate descriptor builder.

**Produces:** One digest-pinned, non-root, minimal image with `/app/candidate.json` and `/app/dashboard`.

- [x] Write static tests first: every `FROM` includes `@sha256:`, `uv sync` uses `--locked`, dashboard uses `npm ci`, final `USER` is numeric non-zero, final `ENTRYPOINT` is fixed, and Docker ignore excludes every forbidden path.

  ```python
  def test_every_base_image_is_digest_pinned() -> None:
      for line in dockerfile_lines():
          if line.startswith("FROM "):
              assert "@sha256:" in line
  ```

- [x] Write runtime-image contract tests that accept an image archive/OCI layout path through `MAAIS_TEST_IMAGE`, inspect files/config without registry access, and assert descriptor hash, UID/GID, labels, entrypoint, absent maps/secrets/tests, and no writable application files.

- [x] Run static tests and confirm Dockerfile is missing.

  ```bash
  uv run pytest -q tests/container/test_dockerfile_contract.py
  ```

- [x] Implement `resolve-base-image-digests.py` against anonymous OCI Registry HTTP APIs for the exact Python 3.12 slim and Node 22 bookworm manifests. It prints immutable references and never invokes Docker, reads Keychain, or requests registry credentials.

  ```bash
  uv run python scripts/resolve-base-image-digests.py
  ```

  Expected: two repository references containing 64-character `sha256` digests; record those exact values in `Dockerfile` in the same commit.

- [x] Implement builders and final stage. The final stage copies the prebuilt virtual environment, package, dashboard assets, migrations, and descriptor; it does not run a package manager.

  ```dockerfile
  ARG RAILWAY_GIT_COMMIT_SHA
  ARG MAAIS_SOURCE_CLEAN
  RUN test "$MAAIS_SOURCE_CLEAN" = "true"
  RUN uv run maais candidate-descriptor \
      --repository /src \
      --dashboard-dir /src/dashboard/dist \
      --git-sha "$RAILWAY_GIT_COMMIT_SHA" \
      --source-clean "$MAAIS_SOURCE_CLEAN" \
      --output /build/candidate.json
  ```

- [x] Make descriptor generation fail when `RAILWAY_GIT_COMMIT_SHA` is not 40 lowercase hexadecimal characters or `MAAIS_SOURCE_CLEAN` is not exactly `true`. Static tests require both `ARG` declarations; the Railway variable contract freezes the clean-source assertion only for Git-provided build contexts, and local builders must prove `git status --porcelain` is empty before passing it.

- [x] Add OCI labels for Git SHA, candidate descriptor schema, and paper-only safety; labels contain no provider/project/account identifiers.

- [ ] Run static tests. Build execution occurs only through an approved credential-free builder, then run runtime-image tests against its exported OCI archive.

  Static contracts, locked dependency builds, OCI parsing, whiteout handling, blob
  verification, and traversal rejection pass locally. The real archive assertion remains
  open until Task 4 supplies the credential-free CI builder and `MAAIS_TEST_IMAGE`.

  ```bash
  uv run pytest -q tests/container/test_dockerfile_contract.py tests/container/test_runtime_image.py
  ```

  Expected: all tests pass; no Docker credential helper is invoked by the tests.

- [x] Commit.

  ```bash
  git add Dockerfile .dockerignore scripts/resolve-base-image-digests.py tests/container/test_dockerfile_contract.py tests/container/test_runtime_image.py
  git commit -m "feat: package railway service roles"
  git push origin feat/paper-platform-baseline
  ```

## Task 3: Add Railway Service Configuration as Code

**Files:**

- Create: `railway/web.toml`
- Create: `railway/worker.toml`
- Create: `railway/operations.toml`
- Create: `railway/migrator.toml`
- Create: `railway/verifier.toml`
- Create: `docs/runbooks/railway-variables.md`
- Modify: `maais/artifacts/configured.py`
- Modify: `maais/cli.py`
- Modify: `maais/config/artifacts.py`
- Modify: `maais/config/observability.py`
- Modify: `maais/config/security.py`
- Modify: `maais/config/settings.py`
- Modify: `maais/platform/services.py`
- Test: `tests/unit/platform/test_railway_configs.py`
- Test: role-scoped configuration, artifact-reader, runtime-identity, and service-entrypoint tests

**Consumes:** Role entrypoints and Railway Docker build.

**Produces:** Explicit build/start/health/restart settings per role and a secret-safe variable contract.

- [x] Write TOML parser tests proving all app roles use the same Dockerfile, explicit start command, one replica, expected health path only for web, restart policy `NEVER`, and no command runs Alembic except migrator.

  ```python
  EXPECTED_START_COMMANDS = {
      "web.toml": "maais cloud-web",
      "worker.toml": "maais cloud-worker",
      "operations.toml": "maais cloud-operations",
      "migrator.toml": "maais cloud-migrate --expected-revision 0022",
      "verifier.toml": "maais cloud-verifier",
  }
  ```

- [x] Run tests and confirm config files are absent.

  ```bash
  uv run pytest -q tests/unit/platform/test_railway_configs.py
  ```

- [x] Create configs using Railway's current config-as-code schema, `builder = "DOCKERFILE"`, `/healthz/ready` for web, bounded health timeout, overlap-disabled deploys, and `restartPolicyType = "NEVER"`.

  All five files validate against the fetched Railway schema with SHA-256
  `38d35a7de8d6fa511895abbcf9a2cac49a12494fd6a9cd2d4228a5b2a8af5e5f`.

- [x] Document exact variable names grouped by shared, web, worker, operations, migrator, verifier, replica store, canonical store, backend Sentry, and public browser Sentry. Mark each as public metadata or secret and name the role(s) allowed to receive it. Direct the operator to run the interactive password-hash/secret generators personally and paste results directly into sealed provider fields; never ask the operator to paste them into chat.

- [x] Explicitly state that exchange variables are absent, the Sentry upload token belongs only in GitHub Actions, the operator password itself is never stored, and secrets must be entered directly in provider consoles.

- [x] Re-run TOML tests and secret scan.

  ```bash
  uv run pytest -q tests/unit/platform/test_railway_configs.py
  uv run detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
  ```

  Expected: all configs parse and no credential is committed.

  Verified with 13 Railway contract tests, role/configuration regression tests, and a
  baseline-aware secret hook over every changed or new file. The final full
  isolated-database suite passed with 1,424 tests and one expected skip.

- [x] Commit (`4530465`).

  ```bash
  git add railway docs/runbooks/railway-variables.md tests/unit/platform/test_railway_configs.py
  git commit -m "feat: define railway service topology"
  git push origin feat/paper-platform-baseline
  ```

## Task 4: Strengthen CI for Release Candidates

**Files:**

- Modify: `.github/workflows/ci.yml`
- Modify: `.secrets.baseline`
- Modify: `scripts/verify_dashboard_assets.py`
- Modify: `tests/container/test_dashboard_assets.py`
- Create: `scripts/verify-release-candidate.sh`
- Create: `tests/unit/platform/test_ci_contract.py`

**Consumes:** Existing quality/test/frontend/security/PostgreSQL jobs, container tests, migration head `0022`, artifact/auth/redaction contracts.

**Produces:** Exact candidate CI gate, migration cycle, container proof, and current action runtimes.

- [x] Write workflow-structure tests requiring backend quality/test/security, frontend/browser, PostgreSQL integration/coverage, migration cycle, artifact conformance, redaction canaries, and container contract jobs. Require least-privilege permissions and forbid secrets in pull-request jobs.

- [x] Write a test that fails on deprecated Node 20 action runtime majors observed by GitHub. During implementation, verify the latest official major release of `actions/checkout`, `actions/setup-node`, and `astral-sh/setup-uv`, then pin the compatible major and record the release URL in the commit notes, not the workflow.

  Verified on 2026-08-09 from the official release pages for
  [`actions/checkout`](https://github.com/actions/checkout/releases),
  [`actions/setup-node`](https://github.com/actions/setup-node/releases),
  [`astral-sh/setup-uv`](https://github.com/astral-sh/setup-uv/releases), and
  [`actions/upload-artifact`](https://github.com/actions/upload-artifact/releases).
  The workflow uses references `v6`, `v7`, `v9.0.0`, and `v7`, respectively. Because
  `setup-uv` v9 is immutable and publishes no floating `v9` alias, it is pinned to the
  official full commit
  `c771a70e6277c0a99b617c7a806ffedaca235ff9`; <!-- pragma: allowlist secret -->
  every step also installs the Dockerfile's exact `uv==0.11.16` tool version.

- [x] Run the test and confirm the current action versions/coverage fail the new contract.

  ```bash
  uv run pytest -q tests/unit/platform/test_ci_contract.py
  ```

- [x] Add a PostgreSQL migration job that upgrades to `0022`, downgrades to `0018`, re-upgrades to `0022`, and runs schema parity/role tests against an isolated `_test` database.

  The exact cycle runs at the start of the existing `postgres-integration` job so the
  repository retains one isolated PostgreSQL service while still failing before coverage
  or browser tests if migration parity or role tests fail.

- [x] Add artifact contract/redaction/security jobs and container static/runtime inspection. Never make the Sentry map upload token available to ordinary test/build steps.

- [x] Implement `verify-release-candidate.sh` as a read-only aggregator that checks exact commit, clean worktree, locks, schema head, descriptor inputs, and required CI job names; it does not deploy.

- [x] Run local workflow-contract tests and all commands mirrored by CI.

  ```bash
  uv run pytest -q tests/unit/platform/test_ci_contract.py tests/container
  uv run ruff format --check .
  uv run ruff check .
  uv run pyright
  uv run pytest -q
  npm --prefix dashboard ci
  npm --prefix dashboard audit --audit-level=high
  npm --prefix dashboard test
  npm --prefix dashboard run typecheck
  npm --prefix dashboard run build
  ```

  Expected: all commands exit `0`.

  Local evidence: the exact 0022-to-0018-to-0022 cycle and 21 schema/role tests passed;
  artifact and redaction suites passed 96 and 43 tests; frontend passed 42 tests,
  typecheck, production build, asset verification, and an audit with zero vulnerabilities;
  the final isolated-database suite passed 1,430 tests with one expected local skip. The
  skipped OCI runtime assertion is purposefully supplied by the credential-free GitHub
  Buildx job and must pass on the exact pushed commit before this task is complete.

- [ ] Commit and push, then wait for exact-commit CI.

  ```bash
  git add .github/workflows/ci.yml scripts/verify-release-candidate.sh tests/unit/platform/test_ci_contract.py
  git commit -m "ci: verify cloud release candidates"
  git push origin feat/paper-platform-baseline
  gh run list --workflow CI --branch feat/paper-platform-baseline --limit 1
  ```

  Expected: the run head SHA equals local `HEAD` and every required job succeeds.

## Task 5: Extend Preflight With Cloud Evidence

**Files:**

- Create: `maais/operations/cloud_preflight.py`
- Create: `maais/operations/cloud_evidence.py`
- Modify: `maais/operations/preflight.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/operations/test_cloud_preflight.py`
- Test: `tests/integration/test_cloud_preflight_state.py`

**Consumes:** Existing 16 preflight checks, descriptor/runtime/run registry, Railway topology snapshot, role probes, auth tests, telemetry canaries, monitor configuration, artifact capabilities/publication, audit chain, and cost/capacity snapshot.

**Produces:** Versioned fail-closed cloud preflight bundle that preserves every existing gate name and adds explicit cloud gates.

- [ ] Freeze the cloud gate names in a test.

  ```python
  EXISTING_PREFLIGHT_GATES = (
      "manifest_mode",
      "runtime_policy",
      "manifest_candidate_identity",
      "repository_clean",
      "repository_identity",
      "run_mode",
      "exchange_credentials_absent",
      "database_schema",
      "stored_manifest",
      "ledger_consistency",
      "restore_drill",
      "dashboard_build",
      "free_disk",
      "fresh_qualification",
      "process_drill_gate",
      "soak_readiness_gate",
  )

  CLOUD_PREFLIGHT_GATES = (
      "railway_identity",
      "european_single_replica_topology",
      "private_service_topology",
      "database_role_probes",
      "operator_auth_boundary",
      "telemetry_redaction_canaries",
      "sentry_delivery",
      "external_monitors",
      "dual_store_retention",
      "audit_chain",
      "cloud_run_registry",
      "restart_sleep_autodeploy_policy",
      "resource_cost_headroom",
  )
  ```

- [ ] Write one failing test per gate plus a passing complete fixture. Missing evidence, stale evidence, wrong candidate/run/environment, unknown gate, duplicate gate, provider exception, or unverified hash must fail.

- [ ] Run tests and confirm cloud preflight is absent.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_preflight.py tests/integration/test_cloud_preflight_state.py
  ```

- [ ] Implement immutable `CloudEvidenceSnapshot` inputs. Runtime evaluation never calls a mutable provider API implicitly; a separate qualification collector captures signed/hashed Railway/Sentry/storage/cost evidence under an operation ID so the evaluator is deterministic.

- [ ] Reuse `evaluate_candidate_preflight` for the original 16 gates, then append cloud gates. Do not rename, reorder, skip, or reinterpret original gates.

- [ ] Include report schema version, candidate/run/manifest hashes, database system identifier hash, service boot IDs, source evidence hashes, evaluated time, and every gate detail. Exclude credentials, account/trade data, and raw provider errors.

- [ ] Publish the preflight bundle through `ArtifactPublisher`; a local file alone cannot pass cloud preflight.

- [ ] Run cloud/local preflight tests and full ledger/qualification regressions.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_preflight.py tests/integration/test_cloud_preflight_state.py tests/unit/operations/test_preflight.py tests/unit/operations/test_qualification.py tests/unit/operations/test_soak_readiness.py
  uv run ruff check maais/operations/cloud_preflight.py maais/operations/cloud_evidence.py maais/operations/preflight.py maais/cli.py tests/unit/operations/test_cloud_preflight.py tests/integration/test_cloud_preflight_state.py
  uv run pyright maais/operations/cloud_preflight.py maais/operations/cloud_evidence.py maais/operations/preflight.py maais/cli.py
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add maais/operations/cloud_preflight.py maais/operations/cloud_evidence.py maais/operations/preflight.py maais/cli.py tests/unit/operations/test_cloud_preflight.py tests/integration/test_cloud_preflight_state.py
  git commit -m "feat: add cloud preflight evidence"
  git push origin feat/paper-platform-baseline
  ```

## Task 6: Add Cloud Process-Drill Evidence

**Files:**

- Create: `maais/operations/cloud_process_drills.py`
- Modify: `maais/operations/process_drills.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/operations/test_cloud_process_drills.py`
- Test: `tests/faults/test_cloud_failure_drills.py`

**Consumes:** Existing process-drill checks, run/service boot registry, health/audit/artifact records, qualification provider-action timeline.

**Produces:** Deterministic drill verdict for Mission Control replacement, worker takeover, operations daily-close replacement, database interruption, artifact target failures, Sentry outage, and backup restore.

- [ ] Freeze preserved local checks and cloud drill names. Cloud evaluation replaces tmux/PID evidence with deployment/boot/lease evidence but keeps candidate, experiment, timeline, projection, ledger, health, incident, and daily-close semantics.

  ```python
  CLOUD_DRILLS = (
      "mission_control_replacement",
      "worker_replacement_lease_takeover",
      "operations_daily_close_replacement",
      "database_interruption_fail_closed",
      "railway_artifact_target_failure",
      "worm_artifact_target_failure",
      "sentry_outage_fallback",
      "backup_restore",
  )
  ```

- [ ] Write failing timeline tests for each drill, including strict lease epoch increase, no duplicate decisions/orders/fills/counterfactuals, exactly one daily report/backup, failure incidents/alerts, idempotent retry, and restored ledger/query reconciliation.

- [ ] Run tests and confirm evaluator is absent.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_process_drills.py tests/faults/test_cloud_failure_drills.py
  ```

- [ ] Implement evaluation over immutable captured observations. It must reject reordered, overlapping, cross-candidate, cross-run, missing-before, missing-after, or unverified events.

- [ ] Implement `cloud-process-drill-verdict` as read-only evaluation plus dual-store publication. It never triggers the provider failure/replacement actions itself.

- [ ] Add a qualification runbook step boundary: before each provider mutation, show exact target and require operator authorization; capture the returned deployment/service identity after action.

- [ ] Run cloud/local process drill, ledger, worker lease, and daily supervisor tests.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_process_drills.py tests/faults/test_cloud_failure_drills.py tests/unit/operations/test_process_drills.py tests/integration/test_operational_state_repository.py tests/unit/operations/test_daily_supervisor.py
  uv run ruff check maais/operations/cloud_process_drills.py maais/operations/process_drills.py maais/cli.py tests/unit/operations/test_cloud_process_drills.py tests/faults/test_cloud_failure_drills.py
  uv run pyright maais/operations/cloud_process_drills.py maais/operations/process_drills.py maais/cli.py
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add maais/operations/cloud_process_drills.py maais/operations/process_drills.py maais/cli.py tests/unit/operations/test_cloud_process_drills.py tests/faults/test_cloud_failure_drills.py
  git commit -m "feat: verify cloud recovery drills"
  git push origin feat/paper-platform-baseline
  ```

## Task 7: Add the Cloud 24-Hour Soak Verdict

**Files:**

- Create: `maais/operations/cloud_soak_readiness.py`
- Modify: `maais/operations/soak_readiness.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/operations/test_cloud_soak_readiness.py`
- Test: `tests/integration/test_cloud_soak_verdict.py`

**Consumes:** Existing 15 soak gates, immutable run/service timeline, internal/external health, artifacts/backups, auth, audit, resource, and decision metadata evidence.

**Produces:** Read-only, dual-store, immutable post-24-hour verdict that never stops the soak or starts the seven-day run.

- [ ] Freeze original and added gate names.

  ```python
  EXISTING_SOAK_GATES = (
      "paper_only_safety",
      "candidate_identity",
      "postgres_cluster_identity",
      "preflight_evidence",
      "pre_soak_process_drills",
      "minimum_duration",
      "process_continuity",
      "runtime_health",
      "ledger_consistency",
      "operational_state",
      "decision_cardinality",
      "decision_metadata_coverage",
      "required_data_quality",
      "structured_logs",
      "daily_report_reconciliation",
  )

  CLOUD_SOAK_GATES = (
      "cloud_identity_continuity",
      "external_monitoring",
      "audit_chain_integrity",
      "dual_store_artifacts",
      "backup_restore_evidence",
      "operator_auth_health",
      "resource_cost_headroom",
  )
  ```

- [ ] Write failing tests for a 23:59:59 run, any extra boot, redeploy/config/scale event, replacement/recovery, schema/cluster/candidate/manifest change, stale minute health, missing monitor sample, queue capacity, error log, Sentry gap, incomplete rationale row, missing daily report/backup/version/retention, auth failure, audit failure, and hard cost cutoff.

- [ ] Write explicit passing tests for 60-bar warm-up quarantine/neutral decisions and zero fills with otherwise complete decision/rejection/counterfactual evidence.

- [ ] Run tests and confirm cloud soak evaluator is absent.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_soak_readiness.py tests/integration/test_cloud_soak_verdict.py
  ```

- [ ] Implement exact 24-hour calculation from authoritative run activation time, require one unchanged boot per required role, and fail permanently on any interruption event even when later health is green.

- [ ] Reuse original soak evaluation for its 15 gates, append cloud gates, include cardinality/rationale/horizon counts, and require the Berlin daily report plus post-close logical backup to reconcile and retain.

- [ ] Make `cloud-soak-verdict` refuse to run before 24 hours, use verifier read-only database credentials, create no trading/control state, publish through both artifact targets, and leave services running.

- [ ] Run cloud/local soak, reports, and final-report regressions.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_soak_readiness.py tests/integration/test_cloud_soak_verdict.py tests/unit/operations/test_soak_readiness.py tests/unit/operations/test_reporting.py tests/unit/operations/test_final_reporting.py
  uv run ruff check maais/operations/cloud_soak_readiness.py maais/operations/soak_readiness.py maais/cli.py tests/unit/operations/test_cloud_soak_readiness.py tests/integration/test_cloud_soak_verdict.py
  uv run pyright maais/operations/cloud_soak_readiness.py maais/operations/soak_readiness.py maais/cli.py
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add maais/operations/cloud_soak_readiness.py maais/operations/soak_readiness.py maais/cli.py tests/unit/operations/test_cloud_soak_readiness.py tests/integration/test_cloud_soak_verdict.py
  git commit -m "feat: add cloud soak readiness verdict"
  git push origin feat/paper-platform-baseline
  ```

## Task 8: Update Operator Documentation and Evidence Schemas

**Files:**

- Modify: `AGENTS.md`
- Modify: `README.md`
- Create: `docs/runbooks/railway-qualification.md`
- Create: `docs/runbooks/railway-production-preflight.md`
- Create: `docs/runbooks/railway-soak.md`
- Create: `docs/runbooks/railway-recovery.md`
- Create: `docs/runbooks/railway-incidents.md`
- Modify: `docs/runbooks/operations.md`
- Modify: `docs/runbooks/recovery.md`
- Test: `tests/unit/platform/test_cloud_runbooks.py`

**Consumes:** All implemented commands, variable contract, provider boundaries, evidence gates.

**Produces:** Exact safe operator flow from local verification through qualification, standby production, 24-hour soak, verdict, and stop-before-seven-day boundary.

- [ ] Write runbook lint tests requiring every command to exist in CLI help, every variable to exist in settings, no exchange/Docker credentials, no raw secrets, no destructive database commands, and explicit approval markers before external mutations and timed-run starts.

- [ ] Run tests and confirm runbooks are absent.

  ```bash
  uv run pytest -q tests/unit/platform/test_cloud_runbooks.py
  ```

- [ ] Document qualification in this exact order: verify exact pushed commit/CI, create candidate descriptor, configure private services/roles, run migration, deploy standby, verify identity/auth/telemetry/storage, create disposable process-drill run, perform authorized drills, publish drill/restore evidence, stop disposable run.

- [ ] Document production in this exact order: promote same candidate, verify descriptor, create fresh standby run/manifest, capture provider snapshot, run cloud preflight, present every gate, request explicit soak authorization, activate once, freeze configuration.

- [ ] Document soak monitoring every minute through durable health data and independent monitor state; do not instruct threshold changes or recovery. Any interruption invalidates the run and requires evidence preservation before a separately authorized replacement.

- [ ] Document final verdict and hard stop before seven-day authorization. Include how to inspect decisions, rejections, proposals, orders, fills, counterfactuals, rationale metadata, daily reports, incidents, logs, Sentry, and artifact versions.

- [ ] Re-run runbook lint plus CLI help and README link tests.

  ```bash
  uv run pytest -q tests/unit/platform/test_cloud_runbooks.py tests/unit/test_cli.py
  uv run maais --help
  ```

  Expected: tests pass and help lists every cloud command.

- [ ] Commit.

  ```bash
  git add AGENTS.md README.md docs/runbooks tests/unit/platform/test_cloud_runbooks.py
  git commit -m "docs: add railway qualification runbooks"
  git push origin feat/paper-platform-baseline
  ```

## Task 9: Run Full Local Release Verification

**Files:**

- No source changes unless a failing gate is reproduced and fixed in a separate test-first commit.
- Evidence output: `artifacts/qualification/` only after the working tree is clean and the exact commit is frozen.

**Consumes:** All five linked plans at migration head `0022`.

**Produces:** Clean exact-commit local evidence and a list of operator-owned external inputs; no deployment.

- [ ] Confirm worktree, branch, commit, and remote identity.

  ```bash
  git status --short --branch
  git rev-parse HEAD
  git rev-parse origin/feat/paper-platform-baseline
  ```

  Expected: clean worktree and all SHAs equal.

- [ ] Run the full backend/frontend/security/container/migration verification matrix from the master plan.

- [ ] Run existing local qualification, backup/restore, process drills, and paper-only dependency-graph checks against the exact commit. Do not start a local or cloud timed soak.

- [ ] Verify GitHub Actions at the exact SHA and capture job identities without treating CI as readiness.

- [ ] Present external inputs still required from the operator: Railway secret entry, Sentry DSNs/monitor configuration, WORM provider/bucket/credentials/retention policy, Railway budget alert/cutoff state, and approval for provider account mutations.

- [ ] Stop here if any required external input is missing. Official cloud preflight must remain failed rather than bypassing the gate.

## Task 10: Provision and Qualify Railway After Explicit Account Authorization

**Files:**

- No repository changes. Provider state changes are recorded as immutable evidence.

**Consumes:** Exact clean candidate, user-authorized Railway project, existing Sentry projects, operator-entered secrets, WORM target.

**Produces:** Passing qualification deployment, restore drill, and cloud process-drill bundle; no production soak.

- [ ] Re-audit the current Railway project/environment/service link, branch, commit, failed deployments, variables, domains, resources, and autodeploy before changing anything. Never reuse an older `main` build.
- [ ] Show the exact services/environment/database/bucket/domain actions and receive explicit operator approval before applying them.
- [ ] Have the operator enter secrets directly in Railway, GitHub, Sentry, and WORM consoles; verify presence/capabilities without reading values back. Qualification and production use separate databases, buckets, credentials, object prefixes, environments, and experiment identities.
- [ ] Provision qualification services private-first, create least-privilege roles, run migrator once, deploy exact candidate in standby, and verify candidate/runtime/schema/cluster/role identity.
- [ ] Expose only Mission Control, verify TLS/auth/CSRF/session/WebSocket/export boundaries, then enable secret uptime and Cron monitors with sole-operator email routing. Trigger controlled non-sensitive failures/check-ins and require the operator to confirm receipt of each email class before the monitoring gate passes.
- [ ] Run Sentry backend/frontend test events and redaction canaries without sensitive data.
- [ ] Verify Railway replica and WORM versioning/Object Lock by publishing and reading back disposable qualification evidence.
- [ ] Create a disposable process-drill run and request approval before each intentional Railway replacement/interruption/provider denial.
- [ ] Run every cloud drill, publish its verified bundle, restore WORM backup into a fresh qualification database, reconcile schema/counts/ledger/read queries, and preserve the restore database until separately authorized cleanup.
- [ ] Stop qualification and report every gate, artifact version, retention deadline, incident, and cost/resource observation.

## Task 11: Promote to Standby and Request 24-Hour Soak Authorization

**Files:**

- No repository changes. Production configuration/evidence is frozen by hashes.

**Consumes:** Passing qualification/drills/restore, same candidate descriptor, fresh production run identity.

**Produces:** Passing production cloud preflight and a clear authorization request; worker remains standby.

- [ ] Promote the exact descriptor/image to production paper with one replica per role, European region, private worker/ops/database/storage, web-only public domain, restart `NEVER`, sleep off, and autodeploy off.
- [ ] Create a fresh manifest/run identity and verify it has no prior decisions.
- [ ] Capture provider/configuration/resource/cost/monitor snapshots and verify a hard spending cutoff cannot terminate 24 hours.
- [ ] Run `cloud-preflight`, publish it to both stores, and report every original and cloud gate.
- [ ] If any gate fails, preserve evidence, keep standby, and fix/requalify through a new commit or authorized provider change.
- [ ] If every gate passes, ask the sole operator explicitly: “Authorize activating this exact run for the uninterrupted 24-hour Railway paper soak?”
- [ ] Do not infer authorization from approval of this plan, Railway access, a deployment, or qualification.

## Task 12: Run the Separately Authorized 24-Hour Soak and Stop

**Files:**

- No source/config/provider changes during the timed run.

**Consumes:** Explicit soak authorization and passing exact-run production preflight.

**Produces:** One uninterrupted run, daily report/backup, immutable passing/failing verdict, and no seven-day start.

- [ ] Activate the exact standby run once and record authoritative UTC/Berlin start, boot IDs, deployment/replica/region, candidate/manifest/schema/cluster identities, configuration snapshot, and earliest verdict time.
- [ ] Monitor minute health, independent uptime, Sentry errors/Cron, queue/cursors/ledger/audit/incidents, decision cardinality/rationale completeness, artifact replication, and resource/cost headroom without mutation.
- [ ] Treat the first 60 prior bars per symbol as expected warm-up quarantine or neutral decisions; treat zero fills as an observation only.
- [ ] Immediately mark the run interrupted on any boot/redeploy/replacement/recovery/scale/config/schema/cluster identity change. Preserve evidence and do not auto-recover.
- [ ] After Berlin midnight, require the daily report and logical backup to reconcile, publish to both targets, and carry verified version/retention metadata.
- [ ] At or after 24 hours, run the read-only verifier once, publish the cloud soak verdict, and report every gate and immutable object version.
- [ ] Do not stop/restart/recover the run as part of verdict generation. Any later stop is a separate operator action.
- [ ] Stop at the authorization boundary and ask separately whether to begin the seven-day paper test.

## Definition of Done

- One digest-pinned non-root candidate image is shared by all roles and contains no forbidden build/runtime files.
- Role entrypoints verify candidate/schema/database role/runtime identity and migrations never run from application startup.
- CI passes at the exact SHA, including migration cycle, container, artifact, auth, redaction, and browser gates.
- Cloud preflight retains all 16 original gates and adds every approved cloud gate.
- Qualification passes all cloud failure/replacement drills and restores an exact WORM backup into a fresh database.
- Production preflight passes for a fresh standby run.
- A separately authorized 24-hour soak runs without any interruption and produces a fully reconciled dual-store verdict, or fails visibly with preserved evidence.
- The platform does not start the seven-day test until the sole operator gives a new explicit authorization.
