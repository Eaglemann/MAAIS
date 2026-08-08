# MAAIS Cloud Identity and Data Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Railway build, service boot, and paper run an immutable verified identity, and enforce PostgreSQL least privilege without changing the paper-only execution boundary.

**Architecture:** A canonical descriptor binds source, locks, schema, agents, dashboard assets, and build definition. Each process derives a fail-closed Railway identity, registers its boot, and verifies the descriptor and schema before readiness. Migration `0019` stores candidates, runs, and service instances; purpose-bound PostgreSQL roles prevent web, verifier, and operations services from gaining worker authority.

**Tech Stack:** Python 3.12, Pydantic Settings, dataclasses, SHA-256 canonical JSON, SQLAlchemy 2, Alembic, PostgreSQL 16, pytest, Hypothesis, Ruff, Pyright.

## Global Constraints

- Preserve the local `Settings` defaults and all three closed `RunMode` values.
- Railway `paper_live` services must fail validation if any exchange credential is non-empty.
- Candidate and runtime IDs are public metadata; database passwords, object-store credentials, monitor secrets, operator credentials, and DSNs use `SecretStr` and never appear in `repr`, logs, errors, or serialized descriptors.
- The worker is the sole trading-state writer. Role bootstrap and migrations run only under the `migrator` role and only outside timed runs.
- A service with a missing Railway identity field, descriptor mismatch, unexpected schema, wrong region, or wrong database role must stay unready and exit non-zero for worker/migrator roles.
- Registration is append-only for candidate and boot identity. Heartbeats may advance monotonically but cannot rewrite an existing boot's immutable identity.
- All timestamps are UTC-aware and all hashes are lowercase 64-character SHA-256 values.

---

## Interfaces Produced

`maais/platform/identity.py` must expose:

```python
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

    @classmethod
    def build(
        cls,
        *,
        git_sha: str,
        source_clean: bool,
        uv_lock_sha256: str,
        dashboard_lock_sha256: str,
        schema_revision: str,
        agent_implementation_hashes: Mapping[str, str],
        dashboard_asset_manifest_sha256: str,
        build_definition_sha256: str,
    ) -> "CandidateDescriptor":
        raise NotImplementedError

    @classmethod
    def from_path(cls, path: Path) -> "CandidateDescriptor":
        raise NotImplementedError

    def to_json_data(self) -> dict[str, JsonValue]:
        raise NotImplementedError


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
```

`maais/platform/registry.py` must expose:

```python
class RunPurpose(StrEnum):
    PROCESS_DRILL = "process_drill"
    SOAK = "soak"
    SEVEN_DAY = "seven_day"


class RunStatus(StrEnum):
    STANDBY = "standby"
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    COMPLETED = "completed"


class CandidateStatus(StrEnum):
    REGISTERED = "registered"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PlatformCandidate:
    descriptor: CandidateDescriptor
    status: CandidateStatus
    creator_deployment_id: str
    registered_at: datetime
    qualifying_at: datetime | None
    qualified_at: datetime | None
    qualification_evidence_hash: str | None


@dataclass(frozen=True, slots=True)
class PlatformRun:
    id: UUID
    experiment_id: UUID
    candidate_hash: str
    manifest_hash: str
    database_system_identifier: str
    railway_environment_id: str
    purpose: RunPurpose
    status: RunStatus
    requested_operator_command_id: UUID | None
    activating_worker_boot_id: UUID | None
    continuity_invalidated: bool
    started_at: datetime | None
    invalidated_at: datetime | None
    invalidation_reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceInstance:
    boot_id: UUID
    run_id: UUID | None
    identity: RailwayRuntimeIdentity
    first_seen_at: datetime
    last_heartbeat_at: datetime
    heartbeat_sequence: int
    stopped_at: datetime | None
    terminal_reason: str | None
```

`PlatformRepository` must provide `register_candidate`, `begin_candidate_qualification`, `qualify_candidate`, `reject_candidate`, `create_run`, `activate_run`, `invalidate_run`, `complete_run`, `register_service_instance`, `heartbeat_service_instance`, `stop_service_instance`, `get_run`, and `list_run_services`. Candidate qualification transitions are monotonic; after `QUALIFIED` or `REJECTED`, identity and evidence fields are immutable.

## Task 1: Add Cloud Configuration Without Weakening Local Defaults

**Files:**

- Create: `maais/config/cloud.py`
- Modify: `maais/config/settings.py`
- Test: `tests/unit/config/test_cloud_settings.py`
- Test: `tests/test_settings.py`

**Consumes:** Existing `RunMode`, local `Settings`, Railway-provided identity variables.

**Produces:** Typed deployment target, service role, region, descriptor path, expected schema, and secret-safe database role settings.

- [x] Write failing tests proving local defaults remain valid, Railway requires every identity field, official paper mode rejects any exchange credential, and secret fields are absent from `repr` and `model_dump(mode="json")`.

  ```python
  def test_railway_paper_settings_require_complete_identity_and_no_exchange_credentials() -> None:
      with pytest.raises(ValidationError, match="RAILWAY_SERVICE_ID"):
          Settings(
              deployment_target="railway",
              run_mode="paper_live",
              service_role="worker",
              _env_file=None,
          )

      with pytest.raises(ValidationError, match="exchange credentials"):
          Settings(
              deployment_target="railway",
              run_mode="paper_live",
              service_role="worker",
              railway_project_id="project",
              railway_environment_id="environment",
              railway_service_id="service",
              railway_deployment_id="deployment",
              railway_snapshot_id="snapshot",
              railway_replica_id="replica",
              railway_region="europe-west4",
              candidate_descriptor_path="/app/candidate.json",
              expected_schema_revision="0019",
              binance_demo_api_key="forbidden",  # pragma: allowlist secret
              _env_file=None,
          )
  ```

- [x] Run the focused tests and confirm they fail because the cloud fields and validators do not exist.

  ```bash
  uv run pytest -q tests/unit/config/test_cloud_settings.py tests/test_settings.py
  ```

  Expected: new Railway configuration assertions fail; existing local settings tests remain green.

- [x] Keep Railway environment variables as top-level `Settings` fields, expose them through a frozen `CloudSettings` view, and add a model-level validator in `Settings` that is inactive for `DeploymentTarget.LOCAL` and fail closed for Railway. This preserves Railway's built-in variable names instead of requiring nested environment syntax.

  ```python
  class CloudSettings(BaseModel):
      model_config = ConfigDict(frozen=True)

      deployment_target: DeploymentTarget = DeploymentTarget.LOCAL
      service_role: ServiceRole | None = None
      railway_project_id: str = ""
      railway_environment_id: str = ""
      railway_service_id: str = ""
      railway_deployment_id: str = ""
      railway_snapshot_id: str | None = None
      railway_replica_id: str = ""
      railway_region: str = ""
      candidate_descriptor_path: Path = Path("/app/candidate.json")
      expected_schema_revision: str = ""
      database_role_name: str = ""


  class Settings(BaseSettings):
      deployment_target: DeploymentTarget = DeploymentTarget.LOCAL
      service_role: ServiceRole | None = None
      railway_project_id: str = ""

      @property
      def cloud(self) -> CloudSettings:
          raise NotImplementedError
  ```

- [x] Add an explicit `Settings.redacted_summary()` allowlist instead of serializing the settings model in logs.

- [x] Re-run focused tests, Ruff, and Pyright.

  ```bash
  uv run pytest -q tests/unit/config/test_cloud_settings.py tests/test_settings.py
  uv run ruff check maais/config tests/unit/config tests/test_settings.py
  uv run pyright maais/config
  ```

  Expected: all commands exit `0`.

- [x] Commit.

  ```bash
  git add maais/config/cloud.py maais/config/settings.py tests/unit/config/test_cloud_settings.py tests/test_settings.py
  git commit -m "feat: add cloud runtime settings"
  git push origin feat/paper-platform-baseline
  ```

## Task 2: Build and Verify the Canonical Candidate Descriptor

**Files:**

- Create: `maais/platform/__init__.py`
- Create: `maais/platform/identity.py`
- Create: `maais/platform/candidate.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/platform/test_candidate_identity.py`
- Fixture: `tests/fixtures/platform/dashboard-assets.json`

**Consumes:** Git SHA supplied by the build, `uv.lock`, `dashboard/package-lock.json`, Alembic head, agent implementation files, built dashboard asset inventory, and `Dockerfile`.

**Produces:** `/app/candidate.json`, canonical `descriptor_hash`, and the `maais candidate-descriptor` command.

- [x] Write failing canonicalization tests: input mapping order cannot change the hash, a single byte change must change it, unknown/missing keys are rejected, a dirty source flag is rejected for official candidates, and reading re-verifies the stored hash.

  ```python
  def test_candidate_descriptor_hash_covers_every_identity_input(tmp_path: Path) -> None:
      first = candidate_descriptor(source_clean=True, git_sha="a" * 40)
      second = candidate_descriptor(source_clean=True, git_sha="b" * 40)
      assert first.descriptor_hash != second.descriptor_hash

      path = tmp_path / "candidate.json"
      write_candidate_descriptor(first, path)
      assert CandidateDescriptor.from_path(path) == first
  ```

- [x] Run the focused test and confirm import failures identify the missing module.

  ```bash
  uv run pytest -q tests/unit/platform/test_candidate_identity.py
  ```

- [x] Implement strict validators and canonical JSON hashing using the existing `maais.domain.json.content_hash`; do not hash the `descriptor_hash` field into itself.

  ```python
  def _descriptor_payload(descriptor: CandidateDescriptor) -> dict[str, JsonValue]:
      return {
          "schema_version": descriptor.schema_version,
          "git_sha": descriptor.git_sha,
          "source_clean": descriptor.source_clean,
          "uv_lock_sha256": descriptor.uv_lock_sha256,
          "dashboard_lock_sha256": descriptor.dashboard_lock_sha256,
          "schema_revision": descriptor.schema_revision,
          "agent_implementation_hashes": dict(sorted(descriptor.agent_implementation_hashes.items())),
          "dashboard_asset_manifest_sha256": descriptor.dashboard_asset_manifest_sha256,
          "build_definition_sha256": descriptor.build_definition_sha256,
      }
  ```

- [x] Implement `build_candidate_descriptor(repository_root, dashboard_dist, git_sha, source_clean)` so it derives all hashes from bytes, discovers the single Alembic head, uses the existing agent implementation hashing rules, and rejects missing or duplicate asset paths.

- [x] Add CLI arguments `--repository`, `--dashboard-dir`, `--git-sha`, `--source-clean`, and `--output`; write atomically with mode `0644` because the descriptor contains no secrets.

- [x] Run focused tests plus manifest identity regressions.

  ```bash
  uv run pytest -q tests/unit/platform/test_candidate_identity.py tests/unit/experiments/test_prepare_live.py tests/unit/experiments/test_manifest.py
  uv run ruff check maais/platform maais/cli.py tests/unit/platform
  uv run pyright maais/platform maais/cli.py
  ```

  Expected: all commands exit `0` and no descriptor field can be omitted without failure.

- [x] Commit.

  ```bash
  git add maais/platform maais/cli.py tests/unit/platform tests/fixtures/platform
  git commit -m "feat: add cloud candidate identity"
  git push origin feat/paper-platform-baseline
  ```

## Task 3: Add Migration 0019 and Platform Models

**Files:**

- Create: `alembic/versions/0019_cloud_platform_registry.py`
- Create: `maais/db/models/platform.py`
- Modify: `maais/db/models/__init__.py`
- Modify: `alembic/env.py`
- Modify: `tests/integration/conftest.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/integration/test_platform_schema.py`
- Test: `tests/unit/db/test_platform_models.py`

**Consumes:** Candidate descriptor and runtime/run domain interfaces.

**Produces:** `platform_candidates`, `run_instances`, and `service_instances` with immutable identities and monotonic lifecycle constraints.

- [x] Write a failing schema parity test and assert the exact columns, foreign keys, unique constraints, checks, and indexes.

  ```python
  PLATFORM_TABLES = (
      "service_instances",
      "run_instances",
      "platform_candidates",
  )


  async def test_platform_schema_matches_models(db_connection: AsyncConnection) -> None:
      await assert_schema_matches_models(db_connection, PLATFORM_TABLES)
  ```

- [x] Run the test and confirm it fails because migration `0019` and models are absent.

  ```bash
  uv run pytest -q tests/integration/test_platform_schema.py
  ```

- [x] Create tables with UUID primary keys, UTC timestamps, JSONB descriptor/runtime evidence, 64-character hash checks, lifecycle checks, unique candidate hash, unique boot ID, and indexes on candidate status, run environment/status, and service heartbeat.

  ```python
  revision: str = "0019"
  down_revision: str | None = "0018"


  def upgrade() -> None:
      op.create_table(
          "platform_candidates",
          sa.Column("descriptor_hash", sa.String(64), primary_key=True),
          sa.Column("git_sha", sa.String(40), nullable=False),
          sa.Column("schema_revision", sa.String(32), nullable=False),
          sa.Column("descriptor_json", postgresql.JSONB(), nullable=False),
          sa.Column("status", sa.String(16), nullable=False),
          sa.Column("creator_deployment_id", sa.String(128), nullable=False),
          sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
          sa.Column("qualifying_at", sa.DateTime(timezone=True), nullable=True),
          sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=True),
          sa.Column("qualification_evidence_hash", sa.String(64), nullable=True),
          sa.CheckConstraint("char_length(descriptor_hash) = 64", name="ck_platform_candidate_hash"),
          sa.CheckConstraint("char_length(git_sha) = 40", name="ck_platform_candidate_git_sha"),
          sa.CheckConstraint("jsonb_typeof(descriptor_json) = 'object'", name="ck_platform_candidate_json"),
      )
  ```

  Continue the migration with the exact `run_instances` and `service_instances` columns defined by `PlatformRun` and `ServiceInstance`. Enforce at most one active official run per Railway environment with a partial unique index; bind activation to an operator command and worker boot; persist `continuity_invalidated` and its immutable reason; allow a nullable snapshot only when Railway does not expose one. The downgrade drops in reverse dependency order.

- [x] Add all three tables to `_PHASE_ONE_TABLES` before `experiments`, and change the CI head assertion from `0018` to `0019` in this commit.

- [x] Run migration upgrade/downgrade/re-upgrade and parity tests against the isolated PostgreSQL test database.

  ```bash
  uv run alembic upgrade head
  uv run pytest -q tests/integration/test_platform_schema.py
  uv run alembic downgrade 0018
  uv run alembic upgrade head
  uv run pytest -q tests/integration/test_platform_schema.py
  ```

  Expected: both test runs pass and `alembic_version.version_num` is `0019`.

- [x] Commit.

  ```bash
  git add alembic/versions/0019_cloud_platform_registry.py maais/db/models/platform.py maais/db/models/__init__.py tests/integration/conftest.py tests/integration/test_platform_schema.py .github/workflows/ci.yml
  git commit -m "feat: add cloud platform registry schema"
  git push origin feat/paper-platform-baseline
  ```

## Task 4: Implement the Run and Service Registry

**Files:**

- Create: `maais/platform/registry.py`
- Create: `maais/db/repositories/platform.py`
- Modify: `maais/db/unit_of_work.py`
- Test: `tests/unit/platform/test_registry_domain.py`
- Test: `tests/integration/test_platform_repository.py`

**Consumes:** `CandidateDescriptor`, `RailwayRuntimeIdentity`, migration `0019`, and existing `UnitOfWork` transaction boundaries.

**Produces:** Append-only candidate registration, explicit run lifecycle, immutable service boot registration, monotonic heartbeat, and durable invalidation.

- [x] Write failing domain tests for candidate qualification transitions, legal run transitions, and invalidation permanence.

  ```python
  def test_invalidated_run_cannot_be_reactivated() -> None:
      run = platform_run(status=RunStatus.STANDBY)
      active = run.activate(NOW)
      invalid = active.invalidate("unexpected_worker_boot", NOW + timedelta(seconds=1))
      with pytest.raises(RunTransitionError, match="invalidated"):
          invalid.activate(NOW + timedelta(seconds=2))
  ```

- [x] Write failing PostgreSQL tests for idempotent identical registration, hash collision rejection, candidate freeze after qualification, duplicate active run per environment rejection, activation without command/worker rejection, heartbeat regression rejection, immutable boot identity, and service continuity queries.

- [x] Run the tests and confirm missing domain/repository behavior.

  ```bash
  uv run pytest -q tests/unit/platform/test_registry_domain.py tests/integration/test_platform_repository.py
  ```

- [x] Implement pure domain transition methods, then repository methods using row locks and PostgreSQL uniqueness as the final concurrency boundary.

  ```python
  class PlatformRepository:
      def __init__(self, session: AsyncSession) -> None:
          self._session = session

      async def register_candidate(
          self,
          descriptor: CandidateDescriptor,
          *,
          registered_at: datetime,
      ) -> CandidateDescriptor:
          raise NotImplementedError

      async def heartbeat_service_instance(
          self,
          *,
          boot_id: UUID,
          sequence: int,
          heartbeat_at: datetime,
      ) -> ServiceInstance:
          raise NotImplementedError
  ```

- [x] Add `platform: PlatformRepository` to `UnitOfWorkContext` without granting it event-ledger mutation authority; platform lifecycle rows are operational evidence, not trading domain events.

- [x] Re-run focused tests and the full integration repository suite.

  ```bash
  uv run pytest -q tests/unit/platform/test_registry_domain.py tests/integration/test_platform_repository.py tests/integration/test_operational_state_repository.py
  uv run ruff check maais/platform maais/db/repositories/platform.py maais/db/unit_of_work.py tests/unit/platform tests/integration/test_platform_repository.py
  uv run pyright maais/platform maais/db/repositories/platform.py maais/db/unit_of_work.py
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add maais/platform/registry.py maais/db/repositories/platform.py maais/db/unit_of_work.py tests/unit/platform/test_registry_domain.py tests/integration/test_platform_repository.py
  git commit -m "feat: register cloud runtime services"
  git push origin feat/paper-platform-baseline
  ```

## Task 5: Enforce Purpose-Bound PostgreSQL Roles

**Files:**

- Create: `maais/db/roles.py`
- Create: `maais/operations/migrations.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/db/test_roles.py`
- Test: `tests/integration/test_database_roles.py`

**Consumes:** Migration head, Railway `ServiceRole`, PostgreSQL advisory locks, and one operator-provided bootstrap connection.

**Produces:** `maais_migrator`, `maais_worker`, `maais_web`, `maais_ops`, and `maais_verifier` roles with tested grants; guarded `cloud-bootstrap-roles` and `cloud-migrate` commands.

- [ ] Write failing SQL-generation tests proving all identifiers are fixed constants, password values are bound parameters, role statements are idempotent, and no generated SQL grants superuser, role creation, replication, bypass RLS, database creation, or schema ownership.

- [ ] Write integration probes proving web/verifier cannot write trading projections, web can write only `maais_auth` and execute the command-enqueue/service-registration functions, operations cannot insert decisions/orders/fills, worker cannot alter schema or artifact catalog, and migrator uses one advisory lock.

  ```python
  ROLE_WRITE_PROBES = {
      "maais_web": "INSERT INTO decision_cycles DEFAULT VALUES",
      "maais_verifier": "INSERT INTO health_evaluations DEFAULT VALUES",
      "maais_ops": "INSERT INTO order_intents DEFAULT VALUES",
      "maais_worker": "ALTER TABLE experiments ADD COLUMN forbidden integer",
  }
  ```

- [ ] Run tests and confirm the roles/commands do not exist.

  ```bash
  uv run pytest -q tests/unit/db/test_roles.py tests/integration/test_database_roles.py
  ```

- [ ] Implement bootstrap in a transaction, use fixed role identifiers and parameterized passwords, and grant `CONNECT` plus table/sequence/function privileges from an explicit allowlist. Set `default_transaction_read_only=on` only for verifier. Web receives projection `SELECT`, DML only in `maais_auth`, and `EXECUTE` on audited command-enqueue/service-registration functions; it receives no direct trading-table DML.

- [ ] Implement narrow `SECURITY DEFINER` functions with fixed `search_path`, explicit `current_user` checks, argument validation, and revoked `PUBLIC` execution for command enqueue and per-role service registration/heartbeat. Test that arbitrary SQL and cross-role boot registration remain denied.

- [ ] Implement migration locking with a fixed 64-bit advisory-lock key and exact expected-revision verification.

  ```python
  MIGRATION_LOCK_KEY = 5_321_109_104_001_922_019


  async def assert_expected_schema(session: AsyncSession, expected: str) -> None:
      actual = str(await session.scalar(text("SELECT version_num FROM alembic_version")))
      if actual != expected:
          raise SchemaIdentityError(f"database schema mismatch: expected={expected} actual={actual}")
  ```

- [ ] Ensure role bootstrap and migration commands refuse to run when any `run_instances.status = 'active'` row exists.

- [ ] Re-run focused tests, full security tests, and a fresh migration cycle.

  ```bash
  uv run pytest -q tests/unit/db/test_roles.py tests/integration/test_database_roles.py tests/test_execution_safety.py
  uv run alembic downgrade 0018
  uv run alembic upgrade head
  uv run ruff check maais/db/roles.py maais/operations/migrations.py maais/cli.py tests/unit/db tests/integration/test_database_roles.py
  uv run pyright maais/db/roles.py maais/operations/migrations.py maais/cli.py
  ```

  Expected: all commands exit `0`; head is `0019`; no paper execution safety regression.

- [ ] Commit.

  ```bash
  git add maais/db/roles.py maais/operations/migrations.py maais/cli.py tests/unit/db/test_roles.py tests/integration/test_database_roles.py
  git commit -m "feat: add least privilege database roles"
  git push origin feat/paper-platform-baseline
  ```

## Task 6: Add Runtime Identity Verification and Registration Commands

**Files:**

- Create: `maais/platform/runtime.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/platform/test_runtime_identity.py`
- Test: `tests/integration/test_runtime_registration.py`

**Consumes:** `Settings`, embedded descriptor, current schema/role/system identifier, and platform repository.

**Produces:** `maais cloud-identity` and reusable `verify_and_register_runtime()` called by every cloud role before readiness.

- [ ] Write failing tests for missing Railway metadata, wrong role, wrong descriptor hash, wrong schema, unexpected region, wrong PostgreSQL role, and duplicate boot ID with different identity.

- [ ] Implement identity construction from the explicit allowlist only; generate `boot_id` once per process and never accept it from an HTTP request.

  ```python
  async def verify_and_register_runtime(
      *,
      settings: Settings,
      session_factory: async_sessionmaker[AsyncSession],
      descriptor: CandidateDescriptor,
      boot_id: UUID,
      started_at: datetime,
      run_id: UUID | None,
  ) -> RailwayRuntimeIdentity:
      raise NotImplementedError
  ```

- [ ] Query `current_user`, `version_num`, and `pg_control_system()`/`pg_control_system().system_identifier` through a read-only transaction where supported; fail if the connected role does not match `service_role`.

- [ ] Make `cloud-identity --json` print only candidate hash, role, deployment/replica/region, schema, database system identifier hash, and boot ID; never print URLs or credentials.

- [ ] Run all cloud identity tests and local compatibility regressions.

  ```bash
  uv run pytest -q tests/unit/platform tests/integration/test_platform_repository.py tests/integration/test_runtime_registration.py tests/test_settings.py
  uv run ruff check maais/platform maais/cli.py tests/unit/platform tests/integration/test_runtime_registration.py
  uv run pyright maais/platform maais/cli.py
  ```

  Expected: all commands exit `0`; local mode does not require Railway variables.

- [ ] Commit.

  ```bash
  git add maais/platform/runtime.py maais/cli.py tests/unit/platform/test_runtime_identity.py tests/integration/test_runtime_registration.py
  git commit -m "feat: verify cloud runtime identity"
  git push origin feat/paper-platform-baseline
  ```

## Definition of Done

- Descriptor re-computation matches the embedded candidate exactly and covers every design input.
- Migration `0019` upgrades, downgrades to `0018`, and re-upgrades cleanly.
- Candidate, run, and boot identity cannot be silently overwritten.
- Service heartbeats are monotonic and an unexpected boot can durably invalidate a timed run.
- Database role probes prove least privilege with PostgreSQL, not only mocked SQL.
- Every cloud role verifies descriptor, schema, role, database cluster, region, and Railway identity before readiness.
- Existing local replay, paper, Mission Control, and preflight tests remain green.
