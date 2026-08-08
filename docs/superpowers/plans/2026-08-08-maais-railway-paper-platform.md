# MAAIS Railway Paper Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the approved Railway design into a secure, observable, recoverable paper-only cloud platform, prove it with qualification and a separately authorized 24-hour soak, and stop before the seven-day run until the sole operator explicitly authorizes it.

**Architecture:** One immutable candidate image serves three long-running Railway roles plus purpose-bound migrator/verifier jobs. Mission Control is the only public service, the paper worker is the only trading-state writer, and operations owns health, reports, backups, and evidence publication. PostgreSQL remains authoritative; a Railway bucket is the operational artifact replica and a versioned S3 Object Lock target is the canonical archive.

**Tech Stack:** Python 3.12, asyncio, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, Pydantic Settings, structlog, Sentry, boto3, Argon2id, React 19, TypeScript, Vite, Vitest, Playwright, Docker, Railway, pytest, Ruff, Pyright, GitHub Actions.

## Global Constraints

- Supported runtime modes remain exactly `replay`, `paper_live`, and `testnet_smoke`; no live-money mode or production-order adapter may be introduced.
- Official Railway paper services contain no Binance Demo/Testnet or production exchange credentials.
- The worker alone may mutate trading projections. Web may read projections, manage only `maais_auth`, register its own boot, and execute the narrow command-enqueue function; verifier is transaction-level read-only; operations is limited to operational state, artifacts, health, incidents, and reports.
- Local token-file Mission Control remains available only in local compatibility mode. Railway requires Argon2id authentication, opaque server-side sessions, CSRF, and authenticated WebSockets and exports.
- Every official artifact must be byte-verified in both the Railway replica and the versioned WORM target before publication succeeds.
- A Railway health check, successful CI run, or Sentry alert is not readiness evidence. Existing preflight, restore, qualification, process-drill, soak, daily, and final-report gates remain authoritative.
- An unexpected restart, redeploy, replacement, scaling change, configuration change, or database identity change permanently invalidates an official timed soak.
- Never weaken worker leases, data-quality gates, risk gates, or strategy thresholds to force trades. Zero fills is an observation.
- Never reset or overwrite the authoritative PostgreSQL database. Restore drills use a new suffix-constrained database.
- Never store secrets, credentials, full database URLs, account values, positions, order quantities, operator input, IP addresses, or user agents in off-platform logs or Sentry.
- Keep commits small and explanatory, push each verified subsystem, and omit co-author trailers.
- Do not provision or deploy until all local gates pass. Do not start the 24-hour soak without explicit authorization. Do not start the seven-day run under this plan.

---

## Source of Truth

- Approved design: `docs/superpowers/specs/2026-08-08-maais-railway-paper-platform-design.md`
- Agent and operator constraints: `AGENTS.md`
- Current local workflow: `README.md` and `docs/runbooks/`
- Existing evidence implementations: `maais/operations/preflight.py`, `maais/operations/process_drills.py`, `maais/operations/soak_readiness.py`, `maais/operations/reporting.py`, and `maais/operations/final_reporting.py`

## Linked Plans and Required Order

| Order | Plan | Primary output | Required before |
| --- | --- | --- | --- |
| 1 | `2026-08-08-maais-cloud-identity-data-authority.md` | Candidate identity, run/service registry, least-privilege database roles | Every cloud service |
| 2 | `2026-08-08-maais-durable-artifacts.md` | Dual-store verified evidence, catalog, backup/restore adapters | Cloud qualification |
| 3 | `2026-08-08-maais-mission-control-security.md` | Authenticated operator boundary and private monitor endpoint | Public domain exposure |
| 4 | `2026-08-08-maais-observability.md` | Redacted JSON logs, Sentry, audit chain, health loop | Unattended operation |
| 5 | `2026-08-08-maais-railway-runtime-readiness.md` | Container, role entrypoints, cloud gates, drills, runbooks | Railway deployment and soak |

The plans are sequential at migration and integration boundaries. Within one plan, execute tasks in order unless the task explicitly names an independent test-only step.

## Cross-Plan Interface Contract

The following names are frozen across all five plans:

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import AsyncIterator, Mapping, Protocol
from uuid import UUID


class DeploymentTarget(StrEnum):
    LOCAL = "local"
    RAILWAY = "railway"


class ServiceRole(StrEnum):
    WEB = "web"
    WORKER = "worker"
    OPERATIONS = "operations"
    VERIFIER = "verifier"
    MIGRATOR = "migrator"


@dataclass(frozen=True, slots=True)
class CandidateDescriptor:
    schema_version: int
    git_sha: str
    source_clean: bool
    uv_lock_sha256: str
    dashboard_lock_sha256: str
    schema_revision: str
    agent_implementation_hashes: Mapping[str, str]
    dashboard_asset_manifest_sha256: str
    build_definition_sha256: str
    descriptor_hash: str


@dataclass(frozen=True, slots=True)
class RailwayRuntimeIdentity:
    project_id: str
    environment_id: str
    service_id: str
    deployment_id: str
    snapshot_id: str | None
    replica_id: str
    region: str
    service_role: ServiceRole
    boot_id: UUID
    candidate_hash: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    etag: str
    version_id: str | None
    retention_mode: str | None
    retain_until: datetime | None


class ArtifactStore(Protocol):
    async def capabilities(self) -> "StoreCapabilities":
        raise NotImplementedError

    async def put_verified(self, request: "PutObjectRequest") -> StoredObject:
        raise NotImplementedError

    async def head(self, key: str, *, version_id: str | None = None) -> StoredObject:
        raise NotImplementedError

    def read_chunks(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        raise NotImplementedError
```

Implementations and tests in the linked plans use these names unchanged.

## Migration Contract

| Revision | Owner | Tables |
| --- | --- | --- |
| `0019` | Cloud identity/data authority | `platform_candidates`, `run_instances`, `service_instances` |
| `0020` | Durable artifacts | `scheduled_operations`, `artifact_records`, `artifact_publication_attempts` |
| `0021` | Mission Control security | `maais_auth.operator_sessions`, `maais_auth.operator_auth_state` |
| `0022` | Observability | `audit_events`, `health_evaluations` |

Every revision must:

- have a reversible downgrade for isolated qualification databases;
- have SQLAlchemy model/schema parity tests;
- be imported by `maais/db/models/__init__.py`;
- be added in dependency-safe order to `tests/integration/conftest.py`;
- update the CI head assertion only in the commit that introduces it; and
- pass an empty-database upgrade, downgrade-to-`0018`, and re-upgrade test.

## Commit Sequence

Use these commit boundaries after their named verification passes:

1. `feat: add cloud candidate identity`
2. `feat: register cloud runtime services`
3. `feat: add least privilege database roles`
4. `feat: add durable artifact stores`
5. `feat: catalog immutable cloud evidence`
6. `feat: secure mission control sessions`
7. `feat: protect mission control surfaces`
8. `feat: add redacted production telemetry`
9. `feat: add cloud health audit loop`
10. `feat: package railway service roles`
11. `feat: add cloud readiness evidence`
12. `docs: add railway qualification runbooks`

Do not combine a failed migration, dependency change, security boundary, or readiness schema change with unrelated cleanup.

## Approved Design Acceptance Coverage

| Design acceptance criterion | Implementation and proof |
| --- | --- |
| Clean candidate passes CI and local compatibility | Master verification matrix; Runtime Tasks 2, 4, and 9 |
| Complete Mission Control surface is private and security-tested | Security Tasks 3–6; Observability Task 8 |
| Worker, web, operations, verifier, and migrator privileges match design | Identity Tasks 3–6 |
| JSON logs retain tracebacks and pass secret canaries | Observability Tasks 2–3 |
| Backend/frontend Sentry resolve exact release without leaks | Observability Tasks 1, 3, and 4 |
| Uptime/Cron email alerts are independently verified | Observability Tasks 6–7; Runtime Task 10 |
| Candidate/run/service/deployment/database identity is immutable and queryable | Identity Tasks 2–6; Observability Task 8 |
| Decisions and metadata remain visible and reconcilable | Existing Mission Control query/report tests; Observability Task 8; Runtime Task 7 |
| Railway and WORM copies pass hash/version/retention verification | Artifact Tasks 2–5 |
| Daily logical backup restores into a fresh target and reconciles | Artifact Task 6; Runtime Tasks 6 and 10 |
| Exact-candidate cloud process drills pass immutably | Runtime Task 6 and Task 10 |
| Production preflight passes all local and cloud gates | Runtime Task 5 and Task 11 |
| Uninterrupted 24-hour soak produces an immutable verdict | Runtime Task 7 and Task 12 |
| Platform remains paper-only without exchange credentials | Every plan's global constraints; Identity Task 1; Runtime Tasks 1, 5, and 7 |
| Seven-day run remains unstarted until separately authorized | Master stop points; Runtime Tasks 11–12 |

## Integrated Verification Matrix

- [ ] Run backend formatting and lint.

  ```bash
  uv run ruff format --check .
  uv run ruff check .
  ```

  Expected: both commands exit `0`.

- [ ] Run static typing.

  ```bash
  uv run pyright
  ```

  Expected: zero errors.

- [ ] Run backend tests including PostgreSQL integration tests.

  ```bash
  uv run pytest -q
  ```

  Expected: all runnable tests pass; PostgreSQL-only tests skip only when the isolated `_test` database is intentionally absent.

- [ ] Run dependency and secret checks.

  ```bash
  uv run pip-audit
  uv run detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
  ```

  Expected: no unresolved high-risk dependency or new secret finding.

- [ ] Run frontend dependency, unit, type, and production build checks.

  ```bash
  npm --prefix dashboard ci
  npm --prefix dashboard audit --audit-level=high
  npm --prefix dashboard test
  npm --prefix dashboard run typecheck
  npm --prefix dashboard run build
  ```

  Expected: all commands exit `0`; `dashboard/dist` contains no `.map` files in the deployable inventory.

- [ ] Build and inspect the final image without invoking any registry credential helper.

  ```bash
  uv run pytest tests/container -q
  ```

  Expected: the repository-level container tests prove a digest-pinned base, non-root user, frozen entrypoint, absent secret/source-map/test files, and matching embedded descriptor. The implementation runbook may use a credential-free builder only after the user approves that execution.

- [ ] Run the migration cycle against an isolated PostgreSQL database whose name ends in `_test`.

  ```bash
  uv run alembic upgrade head
  uv run alembic downgrade 0018
  uv run alembic upgrade head
  ```

  Expected: head is `0022` after the final command and all schema parity tests pass.

- [ ] Run artifact, authentication, redaction, and readiness contract suites explicitly.

  ```bash
  uv run pytest -q tests/unit/artifacts tests/integration/test_artifact_repository.py
  uv run pytest -q tests/unit/security tests/unit/api tests/integration/test_operator_sessions.py
  uv run pytest -q tests/unit/observability tests/integration/test_health_audit_repository.py
  uv run pytest -q tests/unit/operations/test_cloud_preflight.py tests/unit/operations/test_cloud_soak_readiness.py
  ```

  Expected: every suite passes and seeded canary secrets are absent from captured logs and Sentry envelopes.

- [ ] Run the full GitHub Actions workflow at the exact pushed commit.

  ```bash
  gh run list --workflow CI --branch feat/paper-platform-baseline --limit 1
  ```

  Expected: every required job concludes `success`; workflow evidence is recorded by exact commit SHA.

## Provisioning and Timed-Run Stop Points

- [ ] Stop after local verification and present the exact commit, migration head, dependency locks, and remaining external inputs to the operator.
- [ ] Provision Railway qualification only after the operator approves account actions and directly enters secrets in Railway/Sentry/WORM consoles.
- [ ] Deploy qualification in standby, then run identity, auth, storage, Sentry, restore, and process-drill gates.
- [ ] Promote the same candidate descriptor to production paper in standby and run cloud preflight.
- [ ] Stop and ask for explicit authorization before activating the 24-hour soak.
- [ ] After an uninterrupted passing soak, publish the immutable verdict and stop.
- [ ] Ask separately whether to start the seven-day paper test. A response approving the architecture, plans, deployment, or soak is not seven-day authorization.

## Final Definition of Done

This implementation plan is complete only when:

- all five linked plans are implemented and locally verified;
- the exact candidate passes CI, qualification, restore, cloud process drills, and production preflight;
- exact candidate/run/service/database identity, health/audit history, artifact versions/retention, incidents, decisions, rejections, proposals, orders, fills, counterfactuals, and rationale metadata are visible and reconcilable to the sole operator;
- a separately authorized 24-hour Railway soak runs uninterrupted and produces a passing immutable verdict;
- all daily, backup, dual-store, monitoring, audit, and rationale-completeness evidence reconciles; and
- the platform remains stopped at the seven-day authorization boundary.
