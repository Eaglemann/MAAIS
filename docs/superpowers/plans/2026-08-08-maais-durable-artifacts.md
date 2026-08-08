# MAAIS Durable Artifact and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every official report, manifest, evidence bundle, and logical backup independently recoverable, content-addressed, byte-verified, and retention-protected outside ephemeral Railway compute.

**Architecture:** A streaming `ArtifactStore` port has filesystem and S3-compatible adapters. Official publication validates an existing immutable bundle, writes and reads back a Railway operational replica, writes and reads back a versioned Object Lock canonical copy, then commits an append-only PostgreSQL catalog record. Backup and restore reuse the current verified custom-format dump workflow and never overwrite the source database.

**Tech Stack:** Python 3.12, asyncio, boto3, botocore, pathlib, SHA-256, SQLAlchemy 2, Alembic, PostgreSQL 16, pytest, Hypothesis, botocore Stubber, existing MAAIS backup/restore and evidence bundle code.

## Global Constraints

- No artifact operation may depend on a compute-local path after publication returns success.
- Never use mutable `latest` object keys as evidence.
- An existing key is idempotent only when streamed bytes and SHA-256 match exactly; different bytes are a critical collision.
- Official publication fails unless both Railway and canonical targets pass read-back verification and the canonical target proves versioning, Object Lock mode, version ID, and retention deadline.
- Never load a PostgreSQL dump or large artifact into one in-memory `bytes` value.
- Temporary directories use owner-only permissions and are removed after the publication result is durably cataloged or the attempt fails.
- Do not put access keys, endpoint credentials, signed URLs, database URLs, or provider responses containing credentials into artifact metadata, exceptions, logs, or Sentry.
- WORM cleanup, retention shortening, legal-hold changes, and database cleanup are outside this implementation and require separate operator authority.
- Local filesystem mode remains the default for existing local reports and tests; it cannot satisfy official cloud WORM readiness.

---

## Interfaces Produced

`maais/artifacts/models.py` must expose:

```python
class RetentionClass(StrEnum):
    QUALIFICATION_30D = "qualification_30d"
    OPERATIONAL_90D = "operational_90d"
    OFFICIAL_EVIDENCE_365D = "official_evidence_365d"


@dataclass(frozen=True, slots=True)
class StoreCapabilities:
    versioning: bool
    object_lock: bool
    retention_modes: frozenset[str]


@dataclass(frozen=True, slots=True)
class PutObjectRequest:
    key: str
    source_path: Path
    sha256: str
    size_bytes: int
    content_type: str
    retention_mode: str | None
    retain_until: datetime | None


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    etag: str
    version_id: str | None
    retention_mode: str | None
    retain_until: datetime | None


@dataclass(frozen=True, slots=True)
class BundleFile:
    relative_path: str
    sha256: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True, slots=True)
class BundleDescriptor:
    schema_version: int
    report_id: str
    artifact_type: str
    content_hash: str
    directory: Path
    files: tuple[BundleFile, ...]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: UUID
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    artifact_type: str
    report_id: str
    content_hash: str
    size_bytes: int
    media_type: str
    generated_at: datetime
    producing_deployment_id: str
    producing_service_id: str
    operation_id: UUID
    replica_inventory: tuple[StoredObject, ...]
    canonical_inventory: tuple[StoredObject, ...]
    previous_evidence_hash: str | None
    catalog_content_hash: str


@dataclass(frozen=True, slots=True)
class ScheduledOperation:
    id: UUID
    run_id: UUID
    experiment_id: UUID
    operation_type: str
    berlin_date: date
    status: str
    owner_boot_id: UUID
    generated_at: datetime
    attempt: int
    result_artifact_ids: tuple[UUID, ...]
    reason_code: str | None
    started_at: datetime
    completed_at: datetime | None
    content_hash: str
```

`maais/artifacts/store.py` must expose:

```python
class ArtifactStore(Protocol):
    async def capabilities(self) -> StoreCapabilities:
        raise NotImplementedError

    async def put_verified(self, request: PutObjectRequest) -> StoredObject:
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

## Task 1: Add Artifact Dependencies and Secret-Safe Store Settings

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `maais/config/artifacts.py`
- Modify: `maais/config/settings.py`
- Test: `tests/unit/config/test_artifact_settings.py`

**Consumes:** Existing settings and the Railway/WORM provider credentials the operator will later enter directly in Railway.

**Produces:** Two independently validated S3 targets and frozen retention policy without serializable secrets.

- [x] Add failing tests proving cloud official mode requires distinct replica and canonical buckets/credentials, canonical Object Lock expectations cannot be disabled, retention days are exactly `30`, `90`, and `365`, and all credentials remain redacted.

  ```python
  def test_official_cloud_storage_requires_independent_targets() -> None:
      with pytest.raises(ValidationError, match="independent artifact targets"):
          ArtifactSettings(
              replica_bucket="same",
              canonical_bucket="same",
              canonical_object_lock_required=True,
          )
  ```

- [x] Run focused tests and confirm missing settings/dependency failures.

  ```bash
  uv run pytest -q tests/unit/config/test_artifact_settings.py
  ```

- [x] Add the S3 SDK from the lockfile-resolved package index.

  ```bash
  uv add boto3
  ```

- [x] Implement `ArtifactSettings` with `SecretStr` for access/secret/session tokens and an allowlisted `redacted_summary()`.

  ```python
  class RetentionSettings(BaseModel):
      model_config = ConfigDict(frozen=True)

      qualification_days: Literal[30] = 30
      operational_days: Literal[90] = 90
      official_evidence_days: Literal[365] = 365
  ```

- [x] Add a frozen policy mapping: qualification uses `GOVERNANCE` for 30 days; daily reports, audit exports, and logical backups use `COMPLIANCE` for 90 days; manifests, qualification, restore, process-drill, preflight, soak-verdict, and final bundles use `COMPLIANCE` for 365 days.

- [x] Run focused tests, dependency audit, Ruff, and Pyright.

  ```bash
  uv run pytest -q tests/unit/config/test_artifact_settings.py
  uv run pip-audit
  uv run ruff check maais/config tests/unit/config
  uv run pyright maais/config
  ```

  Expected: all commands exit `0`; no secret appears in assertion output or `repr`.

- [x] Commit.

  ```bash
  git add pyproject.toml uv.lock maais/config/artifacts.py maais/config/settings.py tests/unit/config/test_artifact_settings.py
  git commit -m "build: add artifact storage client"
  git push origin feat/paper-platform-baseline
  ```

## Task 2: Define and Conformance-Test the Artifact Store Port

**Files:**

- Create: `maais/artifacts/__init__.py`
- Create: `maais/artifacts/models.py`
- Create: `maais/artifacts/store.py`
- Create: `maais/artifacts/verification.py`
- Create: `tests/contracts/artifacts.py`
- Test: `tests/unit/artifacts/test_models.py`

**Consumes:** File paths and existing content hashes.

**Produces:** Strict object-key, retention, size, stream, and read-back contracts shared by all adapters.

- [x] Write failing tests that reject absolute paths, traversal, backslashes, mutable `latest` segments, empty path components, non-UTC retention, mismatched file size/hash, duplicate bundle paths, symlinks, and files outside the bundle root.

  ```python
  @pytest.mark.parametrize(
      "key",
      ("/absolute", "../escape", "run/latest/report.json", "double//slash", "back\\slash"),
  )
  def test_artifact_key_rejects_unsafe_or_mutable_paths(key: str) -> None:
      with pytest.raises(ValueError):
          validate_object_key(key)
  ```

- [x] Run focused tests and confirm missing modules fail.

  ```bash
  uv run pytest -q tests/unit/artifacts/test_models.py
  ```

- [x] Implement chunked SHA-256 helpers with a fixed default chunk size of 1 MiB, exact byte count verification, MIME allowlisting, and canonical key construction.

  ```python
  def artifact_key(
      *,
      environment: str,
      candidate_hash: str,
      experiment_id: UUID,
      artifact_type: str,
      report_id: str,
      relative_path: str,
  ) -> str:
      key = "/".join(
          (
              "maais",
              environment,
              candidate_hash,
              str(experiment_id),
              artifact_type,
              report_id,
              relative_path,
          )
      )
      validate_object_key(key)
      return key
  ```

- [x] Create a reusable async conformance suite that tests new put, identical retry, collision rejection, `head`, chunked read, missing object, and capability reporting against any `ArtifactStore` fixture.

- [x] Re-run unit tests, Ruff, and Pyright.

  ```bash
  uv run pytest -q tests/unit/artifacts/test_models.py
  uv run ruff check maais/artifacts tests/contracts/artifacts.py tests/unit/artifacts
  uv run pyright maais/artifacts tests/contracts/artifacts.py
  ```

  Expected: all commands exit `0`.

- [x] Commit.

  ```bash
  git add maais/artifacts tests/contracts/artifacts.py tests/unit/artifacts/test_models.py
  git commit -m "feat: define immutable artifact contract"
  git push origin feat/paper-platform-baseline
  ```

## Task 3: Implement Filesystem and S3-Compatible Stores

**Files:**

- Create: `maais/artifacts/filesystem.py`
- Create: `maais/artifacts/s3.py`
- Test: `tests/unit/artifacts/test_filesystem_store.py`
- Test: `tests/unit/artifacts/test_s3_store.py`
- Test: `tests/contracts/test_artifact_stores.py`

**Consumes:** `ArtifactStore`, local root, S3 endpoint/bucket/region credentials, retention request.

**Produces:** Exclusive filesystem object creation and provider-neutral S3 streaming with explicit Object Lock verification.

- [ ] Instantiate the conformance suite for `FilesystemArtifactStore` and write failing tests for symlink escapes, permission mode, atomic exclusive creation, concurrent identical retry, and collision handling.

- [ ] Write S3 tests with botocore `Stubber` for versioning disabled, missing Object Lock, governance/compliance retention, absent version ID, multipart ETag, exact version read, credential-safe errors, and 8 MiB streaming upload chunks.

  ```python
  def test_canonical_capabilities_require_enabled_versioning_and_object_lock() -> None:
      store, stubber = stubbed_s3_store(canonical=True)
      stubber.add_response("get_bucket_versioning", {"Status": "Suspended"}, {"Bucket": "archive"})
      with pytest.raises(StoreCapabilityError, match="versioning is not enabled"):
          asyncio.run(store.capabilities())
  ```

- [ ] Run focused tests and confirm adapter imports fail.

  ```bash
  uv run pytest -q tests/unit/artifacts/test_filesystem_store.py tests/unit/artifacts/test_s3_store.py tests/contracts/test_artifact_stores.py
  ```

- [ ] Implement filesystem writes with `os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`, `fsync`, parent-directory containment checks, and streamed read-back.

- [ ] Implement `S3ArtifactStore` using boto3 calls inside `asyncio.to_thread`, exact `VersionId` reads, `ContentLength`, provider retention APIs, and `before-call` exception redaction. Do not compare ETag to MD5.

  ```python
  class S3ArtifactStore:
      def __init__(
          self,
          *,
          client: S3Client,
          bucket: str,
          canonical: bool,
          chunk_size: int = 8 * 1024 * 1024,
      ) -> None:
          self._client = client
          self._bucket = bucket
          self._canonical = canonical
          self._chunk_size = chunk_size
  ```

- [ ] Ensure `put_verified` always reads back from the returned version and recomputes SHA-256 before returning.

- [ ] Run conformance tests, dependency audit, Ruff, and Pyright.

  ```bash
  uv run pytest -q tests/unit/artifacts tests/contracts/test_artifact_stores.py
  uv run pip-audit
  uv run ruff check maais/artifacts tests/unit/artifacts tests/contracts
  uv run pyright maais/artifacts
  ```

  Expected: all commands exit `0`; provider errors expose only stable error codes.

- [ ] Commit.

  ```bash
  git add maais/artifacts/filesystem.py maais/artifacts/s3.py tests/unit/artifacts tests/contracts/test_artifact_stores.py
  git commit -m "feat: add durable artifact stores"
  git push origin feat/paper-platform-baseline
  ```

## Task 4: Add Migration 0020 and Append-Only Artifact Catalog

**Files:**

- Create: `alembic/versions/0020_artifact_catalog.py`
- Create: `maais/db/models/artifacts.py`
- Create: `maais/db/repositories/artifacts.py`
- Create: `maais/db/repositories/scheduled_operations.py`
- Modify: `maais/db/models/__init__.py`
- Modify: `maais/db/unit_of_work.py`
- Modify: `tests/integration/conftest.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/integration/test_artifact_repository.py`

**Consumes:** Bundle descriptor, replica/canonical `StoredObject` inventories, run/candidate identity.

**Produces:** Durable exactly-once operation keys, publication attempts, and immutable successful artifact records linked by a tamper-evident catalog chain.

- [ ] Write failing schema/repository tests for exact model parity, unique `(run_id, operation_type, berlin_date)` operation keys, stable `generated_at` across takeover, monotonic attempts, one successful record per environment/candidate/experiment/type/report/content hash, immutable provider version IDs, append-only failed attempts, previous-evidence/content-hash chain continuity, and collision rejection.

  ```python
  async def test_successful_artifact_record_is_immutable(
      uow_factory: UnitOfWork,
      published_record: ArtifactRecord,
  ) -> None:
      async with uow_factory.begin() as uow:
          await uow.artifacts.record_publication(published_record)
      changed = replace(published_record, canonical_version_id="different")
      with pytest.raises(ArtifactCatalogConflict):
          async with uow_factory.begin() as uow:
              await uow.artifacts.record_publication(changed)
  ```

- [ ] Run tests and confirm migration/model/repository failures.

  ```bash
  uv run pytest -q tests/integration/test_artifact_repository.py
  ```

- [ ] Create migration `0020` with `scheduled_operations`, `artifact_publication_attempts`, and `artifact_records`; JSONB inventories must be objects/arrays as appropriate, hashes must be 64 characters, status/operation type values are closed, and successful records require both target versions, canonical retention metadata, `previous_evidence_hash`, and catalog-row `content_hash`.

  ```python
  revision: str = "0020"
  down_revision: str | None = "0019"
  ```

- [ ] Implement scheduled operation acquisition under a row/advisory lock. A replacement may take over only when the previous owner boot is durably stopped or terminal; it keeps the same operation ID and `generated_at`, increments `attempt`, and resumes from already verified artifact records.

- [ ] Implement artifact repository writes with an insert-only successful path under a catalog-stream advisory lock, validate the previous evidence hash, and revalidate row content hashes before returning records.

  ```python
  class ArtifactRepository:
      async def start_attempt(self, attempt: ArtifactPublicationAttempt) -> None:
          raise NotImplementedError

      async def fail_attempt(
          self,
          attempt_id: UUID,
          *,
          reason_code: str,
          failed_at: datetime,
      ) -> None:
          raise NotImplementedError

      async def record_publication(self, record: ArtifactRecord) -> ArtifactRecord:
          raise NotImplementedError
  ```

- [ ] Add `artifacts: ArtifactRepository` and `scheduled_operations: ScheduledOperationRepository` to `UnitOfWorkContext`, add tables to test cleanup, and update CI head assertion to `0020`.

- [ ] Run migration cycle and integration tests.

  ```bash
  uv run alembic upgrade head
  uv run pytest -q tests/integration/test_artifact_repository.py
  uv run alembic downgrade 0019
  uv run alembic upgrade head
  uv run pytest -q tests/integration/test_artifact_repository.py
  ```

  Expected: head is `0020`; all tests pass.

- [ ] Commit.

  ```bash
  git add alembic/versions/0020_artifact_catalog.py maais/db/models/artifacts.py maais/db/repositories/artifacts.py maais/db/repositories/scheduled_operations.py maais/db/models/__init__.py maais/db/unit_of_work.py tests/integration/conftest.py tests/integration/test_artifact_repository.py .github/workflows/ci.yml
  git commit -m "feat: catalog immutable cloud evidence"
  git push origin feat/paper-platform-baseline
  ```

## Task 5: Implement Fail-Closed Dual-Store Publication

**Files:**

- Create: `maais/artifacts/bundles.py`
- Create: `maais/artifacts/publisher.py`
- Test: `tests/unit/artifacts/test_bundles.py`
- Test: `tests/integration/test_artifact_publisher.py`

**Consumes:** Existing immutable report directory, run/candidate identity, retention class, two stores, artifact repository.

**Produces:** Verified dual-store publication result and persistent attempt history.

- [ ] Write failing tests for semantic validation before upload, Railway failure, WORM failure, read-back mismatch, missing retention, catalog failure, identical retry, conflicting retry, and temporary-directory cleanup.

- [ ] Assert no success record exists when either target or catalog step fails, while the failed attempt remains visible and retryable.

  ```python
  async def test_publication_cannot_succeed_with_only_replica(
      publisher: ArtifactPublisher,
      canonical_store: FailingArtifactStore,
  ) -> None:
      with pytest.raises(ArtifactPublicationError, match="canonical_put_failed"):
          await publisher.publish(publication_request())
      assert await artifact_records_count() == 0
      assert await failed_attempts_count("canonical_put_failed") == 1
  ```

- [ ] Run tests and confirm publisher behavior is missing.

  ```bash
  uv run pytest -q tests/unit/artifacts/test_bundles.py tests/integration/test_artifact_publisher.py
  ```

- [ ] Implement bundle validation from the existing bundle's own hash manifest plus an independently derived sorted file inventory. Reject symlinks, unexpected files, mismatched IDs, and non-canonical JSON.

- [ ] Implement the eight-step publication state machine without holding a PostgreSQL transaction across network uploads.

  ```python
  class ArtifactPublisher:
      async def publish(self, request: PublicationRequest) -> ArtifactRecord:
          attempt = await self._start_attempt(request)
          try:
              bundle = validate_bundle(request.bundle_directory)
              replica = await self._publish_store(self._replica, bundle, request, canonical=False)
              canonical = await self._publish_store(self._canonical, bundle, request, canonical=True)
              return await self._record_success(attempt, bundle, replica, canonical)
          except Exception as error:
              await self._record_failure(attempt, stable_publication_reason(error))
              raise
  ```

- [ ] If recording the failed attempt itself fails, log both exceptions at the top-level boundary and keep the original non-zero outcome.

- [ ] Run focused tests plus ledger and report bundle regressions.

  ```bash
  uv run pytest -q tests/unit/artifacts tests/integration/test_artifact_publisher.py tests/unit/operations/test_reporting.py tests/unit/operations/test_qualification.py
  uv run ruff check maais/artifacts tests/unit/artifacts tests/integration/test_artifact_publisher.py
  uv run pyright maais/artifacts
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add maais/artifacts/bundles.py maais/artifacts/publisher.py tests/unit/artifacts/test_bundles.py tests/integration/test_artifact_publisher.py
  git commit -m "feat: publish verified dual store evidence"
  git push origin feat/paper-platform-baseline
  ```

## Task 6: Adapt Backups, Reports, and Restore Verification

**Files:**

- Modify: `maais/operations/backups.py`
- Modify: `maais/operations/restores.py`
- Modify: `maais/operations/reporting.py`
- Modify: `maais/operations/daily_supervisor.py`
- Create: `maais/operations/artifact_publication.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/operations/test_cloud_backups.py`
- Test: `tests/unit/operations/test_cloud_reports.py`
- Test: `tests/integration/test_cloud_restore.py`

**Consumes:** Existing local bundle builders and validators, `ArtifactPublisher`, database/run identity.

**Produces:** Cloud publication wrappers, backup producer identity, object-backed restore input, and idempotent daily close.

- [ ] Write failing tests that preserve existing local return types while cloud wrappers require candidate/deployment/run/operation identities and publish only after local semantic/hash verification.

- [ ] Write a restore test that downloads the exact canonical version to a private temporary directory, verifies retention and dump hash before invoking `pg_restore`, requires a target name ending `_restore_test`, and never issues `DROP DATABASE`.

  ```python
  def test_restore_target_must_be_fresh_and_suffix_constrained() -> None:
      with pytest.raises(ValueError, match="_restore_test"):
          validate_restore_target("maais")
  ```

- [ ] Run focused tests and confirm cloud wrappers are absent.

  ```bash
  uv run pytest -q tests/unit/operations/test_cloud_backups.py tests/unit/operations/test_cloud_reports.py tests/integration/test_cloud_restore.py
  ```

- [ ] Extend backup manifests with candidate hash, Railway deployment/replica/region, run ID, database system identifier, operation ID, and artifact schema version; preserve all existing dump, table-count, schema, and ledger checks.

- [ ] Make daily close acquire `scheduled_operations` by `(run_id, "daily_close", berlin_date)`. Persist its `generated_at` before report generation, use it for deterministic retries, resolve already-successful report/backup records instead of creating second objects, and mark complete only when both catalog records are verified.

- [ ] Add `cloud-publish`, `cloud-backup`, and `cloud-restore-verify` commands. `cloud-restore-verify` accepts an artifact record ID and fresh target URL through a secret setting; it does not accept an arbitrary object key.

- [ ] Run local and cloud backup/report/restore tests.

  ```bash
  uv run pytest -q tests/unit/operations/test_backups.py tests/unit/operations/test_restores.py tests/unit/operations/test_reporting.py tests/unit/operations/test_daily_supervisor.py
  uv run pytest -q tests/unit/operations/test_cloud_backups.py tests/unit/operations/test_cloud_reports.py tests/integration/test_cloud_restore.py
  uv run ruff check maais/operations maais/cli.py tests/unit/operations tests/integration/test_cloud_restore.py
  uv run pyright maais/operations maais/cli.py
  ```

  Expected: all commands exit `0`; local filesystem behavior remains unchanged.

- [ ] Commit.

  ```bash
  git add maais/operations/backups.py maais/operations/restores.py maais/operations/reporting.py maais/operations/daily_supervisor.py maais/operations/artifact_publication.py maais/cli.py tests/unit/operations/test_cloud_backups.py tests/unit/operations/test_cloud_reports.py tests/integration/test_cloud_restore.py
  git commit -m "feat: publish cloud reports and backups"
  git push origin feat/paper-platform-baseline
  ```

## Definition of Done

- All adapters pass the same conformance suite.
- Canonical capability checks prove versioning and Object Lock through provider APIs.
- Official publication cannot return success with only one target, unread bytes, missing version ID, missing retention, or missing catalog record.
- Migration `0020` is reversible and schema/model parity passes.
- Logical backups include complete producer identity and retain existing inventory/ledger evidence.
- Restore verification uses an exact version into a fresh suffix-constrained database and never mutates the source.
- Existing local backup, report, qualification, preflight, soak, and final-report tests remain green.
