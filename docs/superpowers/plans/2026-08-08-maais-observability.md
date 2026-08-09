# MAAIS Observability and Health Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture actionable failures and health changes with complete local evidence, privacy-safe Sentry telemetry, independent uptime/Cron signals, and an append-only database audit chain.

**Architecture:** Every process emits one versioned JSON event schema through a central allowlist/redaction pipeline. Backend and browser Sentry reuse the same privacy contract and exact release identity. Migration `0022` stores chained audit events and health evaluations. The operations role evaluates health each minute, deduplicates incidents, sends Sentry check-ins, and serves a minimal secret-header monitor summary without changing trading state.

**Tech Stack:** structlog, Python logging, contextvars, Sentry Python SDK with FastAPI integration, `@sentry/react`, `@sentry/vite-plugin`, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, React/Vite, GitHub Actions, pytest, Vitest.

## Global Constraints

- Structured logs and PostgreSQL incidents remain authoritative when Sentry is unavailable.
- Every terminal service exception is logged with exception type, message, stack, causal chain, stable error code, and whether fail-closed persistence succeeded; the process exits non-zero.
- Validation/operator errors use stable codes without noisy unhandled-error events.
- Redaction runs before JSON rendering and before every Sentry send. Seeded canary secrets must appear nowhere in captured output.
- Off-platform telemetry excludes database URLs, authorization/cookie/CSRF values, Sentry/object-store/Telegram/exchange credentials, account equity, positions, order quantities, raw operator input, IP addresses, user agents, and request bodies.
- Sentry `send_default_pii` is false, session replay is disabled, and errors/critical check-ins are never sampled out.
- Monitoring and alerts may open/update incidents but never restart services, mutate thresholds, acknowledge/resolve incidents, or start/stop runs.
- The monitor endpoint returns only component booleans and HTTP `200`/`503`; it never returns run IDs, SHAs, trades, positions, symbols, account values, provider errors, or timestamps useful for replay.
- Source maps upload exactly once in CI and are deleted before deployable assets are assembled. Railway never receives the Sentry upload token.

---

## Log Schema Produced

`maais/observability/events.py` must expose:

```python
EVENT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    service_role: str
    environment: str
    release: str
    candidate_hash: str
    deployment_id: str
    replica_id: str
    region: str
    boot_id: UUID


ALLOWED_COMMON_FIELDS = frozenset(
    {
        "event_schema_version",
        "timestamp",
        "level",
        "logger",
        "event",
        "service_role",
        "environment",
        "release",
        "candidate_hash",
        "deployment_id",
        "replica_id",
        "region",
        "boot_id",
        "correlation_id",
        "operation_id",
        "experiment_ref",
        "decision_cycle_id",
        "symbol",
        "outcome",
        "duration_ms",
        "retry_count",
        "reason_code",
        "error_code",
        "exception",
    }
)
```

`exception` is a bounded object with `type`, `message`, `stack`, and `causes`; each string is redacted and length-limited.

## Task 1: Add Sentry Dependencies and Telemetry Configuration

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `dashboard/package.json`
- Modify: `dashboard/package-lock.json`
- Create: `maais/config/observability.py`
- Modify: `maais/config/settings.py`
- Test: `tests/unit/config/test_observability_settings.py`

**Consumes:** Exact release/candidate/environment identity, backend DSN, public browser DSN, Cron monitor slugs.

**Produces:** Role-aware Sentry/log settings with zero secret serialization.

- [x] Write failing tests proving production requires JSON logs, release equals 40-character Git SHA, backend DSN is secret, browser DSN is the only client-exposed value, sampling is bounded, and PII/replay cannot be enabled.

- [x] Run tests and confirm missing configuration.

  ```bash
  uv run pytest -q tests/unit/config/test_observability_settings.py
  ```

- [x] Add backend and frontend SDKs from their lockfile-resolved registries.

  ```bash
  uv add 'sentry-sdk[fastapi]'
  npm --prefix dashboard install @sentry/react
  npm --prefix dashboard install --save-dev @sentry/vite-plugin
  ```

- [x] Implement `ObservabilitySettings` with backend `SecretStr`, exact environment/release, explicit trace/profile sample rates defaulting to `0.0` for the qualification candidate, and named daily-close/backup/evidence Cron slugs.

- [x] Run focused tests, audits, Ruff, Pyright, and frontend typecheck.

  ```bash
  uv run pytest -q tests/unit/config/test_observability_settings.py
  uv run pip-audit
  npm --prefix dashboard audit --audit-level=high
  uv run ruff check maais/config tests/unit/config
  uv run pyright maais/config
  npm --prefix dashboard run typecheck
  ```

  Expected: all commands exit `0`.

- [x] Commit.

  ```bash
  git add pyproject.toml uv.lock dashboard/package.json dashboard/package-lock.json maais/config/observability.py maais/config/settings.py tests/unit/config/test_observability_settings.py
  git commit -m "build: add sentry observability clients"
  git push origin feat/paper-platform-baseline
  ```

## Task 2: Implement the Versioned JSON Log and Redaction Pipeline

**Files:**

- Create: `maais/observability/__init__.py`
- Create: `maais/observability/events.py`
- Create: `maais/observability/context.py`
- Create: `maais/observability/redaction.py`
- Modify: `maais/core/logging.py`
- Test: `tests/unit/observability/test_structured_logging.py`
- Test: `tests/unit/observability/test_redaction.py`

**Consumes:** `TelemetryContext`, structlog event dictionaries, standard-library log records, exception chains.

**Produces:** One-line JSON schema, context binding, stable field allowlist, recursive redaction, and bounded exceptions.

- [x] Write failing tests that seed canaries in plain strings, nested mappings/lists, URLs, headers, exception messages/causes/stacks, standard logging, and arbitrary payloads. Assert every output line is valid JSON and contains no canary.

  ```python
  CANARIES = (
      "postgresql://operator:db-secret@example.invalid/maais",  # pragma: allowlist secret
      "Bearer auth-secret",
      "csrf-secret",
      "sentry-auth-secret",
      "AKIAEXAMPLESECRET",
      "telegram-secret",
  )


  def assert_canaries_absent(value: str) -> None:
      for canary in CANARIES:
          assert canary not in value
  ```

- [x] Add tests proving unknown fields are dropped, allowed domain references are bounded, account/order-sensitive keys are masked, oversized strings are truncated, and exceptions retain type/causal order.

- [x] Run tests and confirm the current logging pipeline leaks canaries or lacks schema fields.

  ```bash
  uv run pytest -q tests/unit/observability/test_structured_logging.py tests/unit/observability/test_redaction.py
  ```

- [x] Implement normalization in this fixed order: merge context, add logger/level/timestamp, normalize exception, redact recursively, enforce allowlist/bounds, add schema version, render JSON.

  ```python
  shared_processors = [
      structlog.contextvars.merge_contextvars,
      structlog.stdlib.add_logger_name,
      structlog.stdlib.add_log_level,
      structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
      normalize_exception,
      redact_event,
      enforce_event_contract,
      add_event_schema_version,
  ]
  ```

- [x] Preserve human-readable development logs by applying the same redaction/allowlist before console rendering.

- [x] Bind correlation/operation context with context managers that always clear contextvars in `finally`.

- [x] Run tests, Ruff, Pyright, and a JSONL parsing smoke.

  ```bash
  uv run pytest -q tests/unit/observability
  uv run ruff check maais/observability maais/core/logging.py tests/unit/observability
  uv run pyright maais/observability maais/core/logging.py
  ```

  Expected: all commands exit `0`; every captured production line parses as one JSON object.

- [x] Commit.

  ```bash
  git add maais/observability maais/core/logging.py tests/unit/observability/test_structured_logging.py tests/unit/observability/test_redaction.py
  git commit -m "feat: add redacted production telemetry"
  git push origin feat/paper-platform-baseline
  ```

## Task 3: Add Backend Sentry With the Same Privacy Contract

**Files:**

- Create: `maais/observability/sentry.py`
- Modify: `maais/api/app.py`
- Modify: `maais/cli.py`
- Modify: `maais/live.py`
- Modify: `maais/db/repositories/platform.py`
- Modify: `maais/orchestration/supervisor.py`
- Modify: `maais/operations/daily_supervisor.py`
- Test: `tests/unit/observability/test_sentry.py`
- Test: `tests/unit/orchestration/test_terminal_failures.py`
- Test: `tests/integration/test_platform_repository.py`

**Consumes:** Redaction pipeline, exact release/runtime context, top-level service boundaries.

**Produces:** Privacy-safe backend Sentry events, full terminal exception evidence, and non-zero exits.

- [x] Write a captured-transport test that sends each seeded canary through request headers/body, tags, breadcrumbs, exception values, contexts, and user data; assert removal from the complete serialized envelope.

- [x] Write terminal-boundary tests proving worker failure attempts the existing halt persistence, reports persistence success/failure, captures original and secondary exceptions, and exits non-zero even when Sentry transport fails.

  ```python
  def test_worker_terminal_error_preserves_original_when_halt_persistence_fails() -> None:
      with pytest.raises(SystemExit) as exit_info:
          run_worker_boundary(worker=FailingWorker(), halt=FailingHalt(), sentry=FailingSentry())
      assert exit_info.value.code != 0
      assert captured_event("worker_terminal_failure")["error_code"] == "worker_unhandled_exception"
  ```

- [x] Run tests and confirm missing Sentry boundary behavior.

  ```bash
  uv run pytest -q tests/unit/observability/test_sentry.py tests/unit/orchestration/test_terminal_failures.py
  ```

- [x] Initialize Sentry once per process with `send_default_pii=False`, exact release/environment, zero default traces/profiles, request-body suppression, sensitive-header stripping, and `before_send`/`before_breadcrumb` redaction.

  ```python
  sentry_sdk.init(
      dsn=settings.backend_dsn.get_secret_value(),
      environment=settings.environment,
      release=settings.release,
      send_default_pii=False,
      traces_sample_rate=settings.traces_sample_rate,
      profiles_sample_rate=settings.profiles_sample_rate,
      before_send=redact_sentry_event,
      before_breadcrumb=redact_sentry_breadcrumb,
  )
  ```

- [x] Replace top-level truncated error printing with `logger.exception` plus explicit Sentry capture; keep existing fail-closed database persistence before exit.

- [x] Add a purpose-bound `sentry-test-event` command that emits one stable, non-sensitive event and returns non-zero when capture/flush cannot be confirmed. It is disallowed during an active timed run.

- [x] Run focused tests plus CLI/supervisor regressions.

  ```bash
  uv run pytest -q tests/unit/observability/test_sentry.py tests/unit/orchestration/test_terminal_failures.py tests/unit/orchestration tests/unit/operations/test_daily_supervisor.py
  uv run ruff check maais/observability maais/api/app.py maais/cli.py maais/orchestration/supervisor.py maais/operations/daily_supervisor.py
  uv run pyright maais/observability maais/api/app.py maais/cli.py maais/orchestration/supervisor.py maais/operations/daily_supervisor.py
  ```

  Expected: all commands exit `0`.

- [x] Commit.

  ```bash
  git add maais/observability/sentry.py maais/api/app.py maais/cli.py maais/live.py maais/db/repositories/platform.py maais/orchestration/supervisor.py maais/operations/daily_supervisor.py tests/unit/observability/test_sentry.py tests/unit/orchestration/test_terminal_failures.py tests/integration/test_platform_repository.py
  git commit -m "feat: capture terminal service failures"
  git push origin feat/paper-platform-baseline
  ```

## Task 4: Add Browser Sentry and One-Time Source Map Release

**Files:**

- Create: `dashboard/src/observability.ts`
- Modify: `dashboard/src/main.tsx`
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/vite.config.ts`
- Modify: `dashboard/package.json`
- Modify: `.github/workflows/ci.yml`
- Modify: `.secrets.baseline`
- Create: `dashboard/scripts/write-asset-manifest.mjs`
- Create: `scripts/verify_dashboard_assets.py`
- Test: `dashboard/src/observability.test.ts`
- Test: `tests/container/test_dashboard_assets.py`

**Consumes:** Public browser DSN, exact Git release/environment, React error boundary, CI-only Sentry upload token.

**Produces:** Redacted frontend errors, release source maps uploaded once by CI, and source-map-free deployable assets.

- [x] Write frontend tests that send canaries through exceptions, breadcrumbs, request data, tags, and component metadata; assert no PII, session replay, local/session storage, cookies, auth headers, query strings, or raw response bodies leave the browser.

- [x] Add an error-boundary test that renders an operator-safe correlation code while capturing the redacted exception.

- [x] Write an asset-inventory test that fails when any `.map` file exists in the final deployable dashboard directory.

- [x] Run tests and confirm missing frontend observability and current source-map behavior.

  ```bash
  npm --prefix dashboard test -- observability.test.ts
  uv run pytest -q tests/container/test_dashboard_assets.py
  ```

- [x] Initialize `@sentry/react` only when DSN/release/environment are present, with `sendDefaultPii: false`, no Replay integration, zero traces by default, `beforeSend`, and `beforeBreadcrumb`.

- [x] Keep Vite source maps in a CI-only staging directory. Add a single release job that runs only on pushed non-fork commits with `SENTRY_AUTH_TOKEN`, uploads maps to `maais-mission-control`, verifies the release, deletes all maps, and writes a hashed asset manifest for the image build to verify in Task 8.

  ```yaml
  env:
    SENTRY_AUTH_TOKEN: ${{ secrets.SENTRY_AUTH_TOKEN }}
  if: github.event_name == 'push' && env.SENTRY_AUTH_TOKEN != ''
  ```

- [x] Ensure Railway build receives the already map-free asset inventory or independently builds with source map output disabled; the upload token is never defined in Railway.

- [x] Run frontend tests/typecheck/build and inventory test.

  ```bash
  npm --prefix dashboard test
  npm --prefix dashboard run typecheck
  npm --prefix dashboard run build
  uv run pytest -q tests/container/test_dashboard_assets.py
  ```

  Expected: all commands exit `0`; final public assets contain no `.map`.

- [ ] Commit.

  ```bash
  git add dashboard/src/observability.ts dashboard/src/main.tsx dashboard/src/App.tsx dashboard/vite.config.ts dashboard/src/observability.test.ts dashboard/scripts/write-asset-manifest.mjs dashboard/package.json scripts/verify_dashboard_assets.py .github/workflows/ci.yml .secrets.baseline tests/container/test_dashboard_assets.py
  git commit -m "feat: add mission control error telemetry"
  git push origin feat/paper-platform-baseline
  ```

## Task 5: Add Migration 0022, Audit Chain, and Health Evaluations

**Files:**

- Create: `alembic/versions/0022_health_audit_events.py`
- Create: `maais/db/models/observability.py`
- Create: `maais/observability/audit.py`
- Create: `maais/db/repositories/observability.py`
- Modify: `maais/api/security.py`
- Modify: `maais/api/app.py`
- Modify: `maais/db/models/__init__.py`
- Modify: `maais/db/unit_of_work.py`
- Modify: `tests/integration/conftest.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/unit/observability/test_audit_chain.py`
- Test: `tests/integration/test_health_audit_repository.py`

**Consumes:** Runtime/run identity, stable event codes, component health results.

**Produces:** Append-only hash-chained audit events and immutable health evaluation snapshots.

- [x] Write failing tests for genesis/next hashes, deterministic canonicalization, sequence gaps, previous-hash mismatch, concurrent append serialization, immutable health checks, and full-chain verification.

  ```python
  def test_audit_hash_binds_previous_hash_and_payload() -> None:
      first = AuditEvent.create(sequence=1, previous_hash=None, payload={"event": "boot"}, occurred_at=NOW)
      second = AuditEvent.create(sequence=2, previous_hash=first.content_hash, payload={"event": "ready"}, occurred_at=NOW)
      assert second.previous_hash == first.content_hash
      assert second.content_hash != first.content_hash
  ```

- [x] Run tests and confirm missing migration/domain/repository failures.

  ```bash
  uv run pytest -q tests/unit/observability/test_audit_chain.py tests/integration/test_health_audit_repository.py
  ```

- [x] Create `audit_events` with global monotonically increasing sequence, previous/content hash checks, bounded actor/event/reason fields, JSONB evidence, runtime/run references, and append-only privileges. Create `health_evaluations` with evaluation ID, run/boot identity, overall status, failed-check names, severity, deduplication key, incident link, recovery reference/time, component JSON, checked time, and content hash.

  ```python
  revision: str = "0022"
  down_revision: str | None = "0021"
  ```

- [x] Implement audit append using `pg_advisory_xact_lock` and verification that recomputes the complete chain in sequence order.

- [ ] Append audit events for login success/rejection/lockout, logout, session expiry/revocation, CSRF rejection, operator command enqueue, service boot/stop, run lifecycle, daily close, backup, restore, artifact publication, and readiness verdict. Store stable codes and pseudonymous actor/session references only.

- [x] Add fixed-search-path `SECURITY DEFINER` append functions for the web and worker event subsets, revoke `PUBLIC`, validate the original login role and allowed event codes, and reconcile grants after migration. Operations may append the approved operational subset; no runtime role receives direct update/delete authority on `audit_events`.

- [x] Add `observability: ObservabilityRepository` to `UnitOfWorkContext`, add tables to cleanup, and update CI head assertion to `0022`.

- [x] Run migration cycle and tests.

  ```bash
  uv run alembic upgrade head
  uv run pytest -q tests/unit/observability/test_audit_chain.py tests/integration/test_health_audit_repository.py
  uv run alembic downgrade 0021
  uv run alembic upgrade head
  uv run pytest -q tests/integration/test_health_audit_repository.py
  ```

  Expected: head is `0022`; all tests pass.

- [ ] Commit.

  ```bash
  git add alembic/versions/0022_health_audit_events.py maais/db/models/observability.py maais/observability/audit.py maais/db/repositories/observability.py maais/api/security.py maais/api/app.py maais/db/models/__init__.py maais/db/unit_of_work.py tests/integration/conftest.py tests/unit/observability/test_audit_chain.py tests/integration/test_health_audit_repository.py .github/workflows/ci.yml
  git commit -m "feat: add health audit chain"
  git push origin feat/paper-platform-baseline
  ```

## Task 6: Build the One-Minute Operations Health Loop

**Files:**

- Create: `maais/operations/cloud_health.py`
- Create: `maais/operations/health_supervisor.py`
- Modify: `maais/operations/health.py`
- Modify: `maais/monitoring/alerting.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/operations/test_cloud_health.py`
- Test: `tests/integration/test_health_supervisor.py`

**Consumes:** Existing ledger/lease/checkpoint/cursor/recovery/incident/kill-switch checks plus service registry, queue, schema/cluster, audit chain, artifacts, daily close, backup, and Sentry state.

**Produces:** Immutable minute evaluations, deduplicated incident transitions, Sentry check-ins, and the `cloud-operations` loop.

- [ ] Write failing table-driven tests for every critical and warning condition, healthy recovery, incident deduplication, Sentry outage fallback, monotonic evaluation times, and no trading/control mutation.

  ```python
  CRITICAL_COMPONENTS = {
      "worker_continuity",
      "worker_lease",
      "database",
      "schema_identity",
      "cluster_identity",
      "ledger",
      "required_cursors",
      "dispatch_queue_capacity",
      "deployment_identity",
      "daily_close",
      "backup",
      "worm_replication",
      "audit_chain",
  }
  ```

- [ ] Run tests and confirm cloud health behavior is absent.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_health.py tests/integration/test_health_supervisor.py
  ```

- [ ] Implement `CloudHealthEvaluator.evaluate(run_id, checked_at)` as a read-only snapshot followed by a separate operational transaction that inserts the immutable result and opens/updates/recovery-marks deduplicated incidents.

- [ ] Implement the supervisor with a monotonic one-minute cadence, advisory ownership lock, graceful SIGTERM, terminal exception boundary, and no catch-up burst after a long scheduling gap.

- [ ] Add Sentry Cron check-ins around daily close, backup, and evidence replication; check-in failure degrades readiness but cannot suppress the operation's local/database result.

- [ ] Add `cloud-operations` CLI command and refuse startup unless runtime identity, role, schema, audit chain, and artifact settings validate.

- [ ] Run health tests plus existing health/incident/worker safety regressions.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_health.py tests/integration/test_health_supervisor.py tests/unit/operations/test_health.py tests/integration/test_operational_state_repository.py tests/test_execution_safety.py
  uv run ruff check maais/operations maais/monitoring maais/cli.py tests/unit/operations tests/integration/test_health_supervisor.py
  uv run pyright maais/operations maais/monitoring maais/cli.py
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add maais/operations/cloud_health.py maais/operations/health_supervisor.py maais/operations/health.py maais/monitoring/alerting.py maais/cli.py tests/unit/operations/test_cloud_health.py tests/integration/test_health_supervisor.py
  git commit -m "feat: add cloud health audit loop"
  git push origin feat/paper-platform-baseline
  ```

## Task 7: Add Minimal Public Health and Secret Monitor Endpoints

**Files:**

- Create: `maais/api/health.py`
- Modify: `maais/api/app.py`
- Modify: `maais/api/schemas.py`
- Test: `tests/unit/api/test_cloud_health_endpoints.py`
- Test: `tests/integration/test_monitor_health_api.py`

**Consumes:** Process boot/readiness state, latest health evaluation, independent monitor secret.

**Produces:** `/healthz/live`, `/healthz/ready`, and `/monitor/v1/health` with strict disclosure boundaries.

- [ ] Write failing response-schema snapshots. Liveness is process-only; readiness is dependency-aware but generic; monitor returns exactly the approved booleans.

  ```python
  MONITOR_COMPONENTS = {
      "database",
      "worker",
      "ledger",
      "cursors",
      "operations",
      "evidence_replication",
      "daily_close",
  }
  ```

- [ ] Test missing/wrong monitor header with constant `404` or `401`, `Cache-Control: no-store`, rate limiting, no operator-session equivalence, and no secrets/identifiers across both healthy and failed responses.

- [ ] Run tests and confirm endpoints are absent.

  ```bash
  uv run pytest -q tests/unit/api/test_cloud_health_endpoints.py tests/integration/test_monitor_health_api.py
  ```

- [ ] Implement constant-time comparison of `X-MAAIS-Monitor-Token`, an independent in-memory rate limit, generic stable response bodies, and HTTP `503` when any critical component is false.

- [ ] Ensure `/healthz/ready` becomes false on schema/candidate mismatch or unavailable database and does not query/return trading state.

- [ ] Run all API security and health tests.

  ```bash
  uv run pytest -q tests/unit/api tests/integration/test_monitor_health_api.py tests/integration/test_mission_control_surface_security.py
  uv run ruff check maais/api tests/unit/api tests/integration/test_monitor_health_api.py
  uv run pyright maais/api
  ```

  Expected: all commands exit `0` and monitor payload keys exactly equal `MONITOR_COMPONENTS` plus `status`.

- [ ] Commit.

  ```bash
  git add maais/api/health.py maais/api/app.py maais/api/schemas.py tests/unit/api/test_cloud_health_endpoints.py tests/integration/test_monitor_health_api.py
  git commit -m "feat: expose minimal cloud health"
  git push origin feat/paper-platform-baseline
  ```

## Task 8: Make Cloud Evidence Visible in Authenticated Mission Control

**Files:**

- Create: `maais/api/cloud_queries.py`
- Modify: `maais/api/schemas.py`
- Modify: `maais/api/app.py`
- Create: `dashboard/src/CloudOperations.tsx`
- Modify: `dashboard/src/api.ts`
- Modify: `dashboard/src/types.ts`
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/src/styles.css`
- Test: `tests/integration/test_cloud_operations_api.py`
- Test: `dashboard/src/CloudOperations.test.tsx`
- Test: `dashboard/src/api.test.ts`

**Consumes:** Candidate/run/service registry, database identity, health evaluations, audit chain, artifact catalog, incidents, and existing experiment/decision views.

**Produces:** Authenticated, paginated, no-store views of exact cloud identity and evidence with links back to decisions, trades, rationale metadata, incidents, reports, and exports.

- [ ] Write failing API tests for `GET /api/v1/platform/candidates/{candidate_hash}`, `GET /api/v1/runs/{run_id}`, and paginated `/services`, `/health`, `/artifacts`, and `/audit` routes. Require authentication, stable cursors, run scoping, no-store headers, and exact content-hash verification before response.

- [ ] Add disclosure tests that allow the operator to see exact deployment/replica/boot/schema/database-cluster identity and artifact version/retention evidence, while rejecting DSNs, credentials, cookies, tokens, password hashes, provider secrets, raw exception payloads, IP addresses, and user agents.

- [ ] Run API tests and confirm the cloud query layer is absent.

  ```bash
  uv run pytest -q tests/integration/test_cloud_operations_api.py
  ```

- [ ] Implement read-only snapshot queries with a fixed maximum page size, keyset cursors, candidate/run ownership checks, and recomputation of candidate, artifact, audit, and health content hashes.

  ```python
  class CloudOperationsQueryService:
      def __init__(self, session: AsyncSession) -> None:
          self._session = session

      async def get_run(self, run_id: UUID) -> CloudRunView:
          raise NotImplementedError

      async def list_audit_events(
          self,
          run_id: UUID,
          *,
          before_sequence: int | None,
          limit: int,
      ) -> AuditEventPage:
          raise NotImplementedError
  ```

- [ ] Write failing frontend tests for an “Operations evidence” view showing candidate/run/database identity, current required boot continuity, minute health history, incidents, artifact target/version/retention status, audit timeline, and links to the existing decisions/trades/research pages.

- [ ] Add explicit UI states for standby, active, continuity invalidated, interrupted, failed evidence replication, stale health, no fills, and incomplete decision rationale. No-fills is informational unless another gate fails.

- [ ] Implement the view with authenticated same-origin clients and resumable pagination. Do not stream routine logs into PostgreSQL; link the operator to the exact Railway/Sentry release/deployment context documented in the runbook.

- [ ] Run backend/frontend tests, typecheck, and build.

  ```bash
  uv run pytest -q tests/integration/test_cloud_operations_api.py tests/integration/test_mission_control_surface_security.py
  npm --prefix dashboard test -- CloudOperations.test.tsx api.test.ts
  npm --prefix dashboard run typecheck
  npm --prefix dashboard run build
  ```

  Expected: all commands exit `0`; every displayed cloud fact comes from an authenticated, hash-verified query.

- [ ] Commit.

  ```bash
  git add maais/api/cloud_queries.py maais/api/schemas.py maais/api/app.py dashboard/src/CloudOperations.tsx dashboard/src/api.ts dashboard/src/types.ts dashboard/src/App.tsx dashboard/src/styles.css tests/integration/test_cloud_operations_api.py dashboard/src/CloudOperations.test.tsx dashboard/src/api.test.ts
  git commit -m "feat: show cloud operations evidence"
  git push origin feat/paper-platform-baseline
  ```

## Definition of Done

- Every production stdout line conforms to schema version `1`, parses as JSON, and passes canary redaction.
- Backend/frontend Sentry events use exact release/environment, carry no forbidden data, and cannot suppress local/database failure evidence.
- Terminal exceptions retain stack and cause information and exit non-zero after fail-closed persistence attempts.
- Migration `0022`, audit-chain verification, and immutable health snapshots pass PostgreSQL concurrency tests.
- Operations evaluates all required components each minute and records failure and recovery without automatic mutation/restart.
- Public health disclosure is minimal; the monitor secret grants no other access.
- The sole operator can query exact candidate, run, service, database, health, audit, incident, and artifact evidence in authenticated Mission Control and follow it to existing decision/trade/rationale views.
- CI uploads frontend maps once, deletes them, and the deployable image contains none.
