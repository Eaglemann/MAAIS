from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from maais.artifacts.models import (
    ArtifactPutDisposition,
    ArtifactPutResult,
    ArtifactType,
    StoreCapabilities,
    StoredArtifact,
)
from maais.artifacts.publisher import (
    ArtifactPublicationError,
    ArtifactPublisher,
    PublicationRequest,
)
from maais.artifacts.store import ArtifactStoreError
from maais.artifacts.verification import hash_file
from maais.db.models.artifacts import ArtifactPublicationAttemptModel, ArtifactRecordModel
from maais.db.repositories.artifacts import ArtifactRepository
from maais.db.unit_of_work import UnitOfWork
from tests.integration.test_artifact_repository import (
    EXPERIMENT_ID,
    NOW,
    OPERATION_ID,
    RUN_ID,
    _acquire_operation,
    _prepare_authority,
)
from tests.integration.test_platform_repository import _descriptor

pytestmark = pytest.mark.integration

REPORT_ID = "a" * 64
PUBLISHED_AT = NOW + timedelta(minutes=3)


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _bundle(root: Path, *, status: str = "ready") -> Path:
    root.mkdir()
    report = _canonical_json({"report_id": REPORT_ID, "status": status})
    (root / "report.json").write_bytes(report)
    manifest = {
        "artifacts": {
            "report.json": {
                "bytes": len(report),
                "sha256": hashlib.sha256(report).hexdigest(),
            }
        },
        "report_id": REPORT_ID,
        "report_schema_version": 1,
    }
    (root / "bundle-manifest.json").write_bytes(_canonical_json(manifest))
    return root


class MemoryArtifactStore:
    def __init__(
        self,
        *,
        canonical: bool,
        fail_put: bool = False,
        corrupt_read: bool = False,
        omit_version: bool = False,
        invalid_capabilities: bool = False,
    ) -> None:
        self.canonical = canonical
        self.fail_put = fail_put
        self.corrupt_read = corrupt_read
        self.omit_version = omit_version
        self.invalid_capabilities = invalid_capabilities
        self.put_calls = 0
        self.objects: dict[str, tuple[StoredArtifact, bytes]] = {}

    async def capabilities(self) -> StoreCapabilities:
        return StoreCapabilities(
            store_name="canonical" if self.canonical else "replica",
            immutable_create=True,
            exact_version_reads=self.canonical,
            versioning_enabled=self.canonical,
            object_lock_enabled=self.canonical,
            compliance_retention_supported=self.canonical and not self.invalid_capabilities,
        )

    async def put_verified(self, request: object) -> ArtifactPutResult:
        from maais.artifacts.models import ArtifactWriteRequest

        assert isinstance(request, ArtifactWriteRequest)
        self.put_calls += 1
        if self.fail_put:
            raise ArtifactStoreError("simulated provider failure")
        digest = hash_file(request.source_path)
        assert (digest.sha256, digest.size_bytes) == (request.sha256, request.size_bytes)
        existing = self.objects.get(request.key)
        if existing is not None:
            return ArtifactPutResult(
                artifact=existing[0],
                disposition=ArtifactPutDisposition.IDENTICAL_RETRY,
            )
        content = request.source_path.read_bytes()
        stored = StoredArtifact(
            store_name="canonical" if self.canonical else "replica",
            key=request.key,
            etag=request.sha256,
            version_id=(
                None
                if self.omit_version or not self.canonical
                else f"version-{len(self.objects) + 1}"
            ),
            sha256=request.sha256,
            size_bytes=request.size_bytes,
            content_type=request.content_type,
            retention=request.retention,
            stored_at=NOW + timedelta(minutes=2),
        )
        self.objects[request.key] = (stored, content)
        return ArtifactPutResult(
            artifact=stored,
            disposition=ArtifactPutDisposition.CREATED,
        )

    async def head(self, key: str, *, version_id: str | None = None) -> StoredArtifact:
        stored = self.objects[key][0]
        if version_id is not None:
            assert stored.version_id == version_id
        return stored

    async def read_chunks(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        del version_id
        content = self.objects[key][1]
        yield b"corrupt" if self.corrupt_read else content


class UUIDSequence:
    def __init__(self) -> None:
        self.value = 100

    def __call__(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


def _request(directory: Path) -> PublicationRequest:
    return PublicationRequest(
        bundle_directory=directory,
        environment="qualification",
        candidate_hash=_descriptor().descriptor_hash,
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        artifact_type=ArtifactType.DAILY_REPORT,
        report_id=REPORT_ID,
        generated_at=NOW,
        producing_deployment_id="deployment-1",
        producing_service_id="operations-1",
    )


def _publisher(
    uow_factory: UnitOfWork,
    replica: MemoryArtifactStore,
    canonical: MemoryArtifactStore,
) -> ArtifactPublisher:
    return ArtifactPublisher(
        replica=replica,
        canonical=canonical,
        uow_factory=uow_factory,
        now=lambda: PUBLISHED_AT,
        uuid_factory=UUIDSequence(),
    )


async def _prepare(uow_factory: UnitOfWork) -> None:
    await _prepare_authority(uow_factory)
    await _acquire_operation(uow_factory)


async def _counts(uow_factory: UnitOfWork) -> tuple[int, int, list[str]]:
    async with uow_factory.begin() as uow:
        records = int(
            await uow.session.scalar(select(func.count()).select_from(ArtifactRecordModel)) or 0
        )
        attempts = int(
            await uow.session.scalar(
                select(func.count()).select_from(ArtifactPublicationAttemptModel)
            )
            or 0
        )
        reasons = list(
            await uow.session.scalars(
                select(ArtifactPublicationAttemptModel.reason_code)
                .where(ArtifactPublicationAttemptModel.reason_code.is_not(None))
                .order_by(ArtifactPublicationAttemptModel.attempt)
            )
        )
    return records, attempts, [str(value) for value in reasons]


async def test_dual_store_publication_catalogs_only_after_both_readbacks(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)

    record = await _publisher(uow_factory, replica, canonical).publish(
        _request(_bundle(tmp_path / "bundle"))
    )

    assert len(record.replica_inventory) == len(record.canonical_inventory) == 2
    assert all(item.version_id for item in record.canonical_inventory)
    assert await _counts(uow_factory) == (1, 1, [])


async def test_semantically_invalid_bundle_fails_before_attempt_or_upload(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)
    directory = _bundle(tmp_path / "bundle")
    (directory / "report.json").write_bytes(b"tampered")

    with pytest.raises(ArtifactPublicationError, match="bundle_invalid"):
        await _publisher(uow_factory, replica, canonical).publish(_request(directory))

    assert await _counts(uow_factory) == (0, 0, [])
    assert replica.put_calls == canonical.put_calls == 0


async def test_capability_failure_is_persisted_before_any_upload(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True, invalid_capabilities=True)

    with pytest.raises(ArtifactPublicationError, match="canonical_capability_failed"):
        await _publisher(uow_factory, replica, canonical).publish(
            _request(_bundle(tmp_path / "bundle"))
        )

    assert await _counts(uow_factory) == (0, 1, ["canonical_capability_failed"])
    assert replica.put_calls == canonical.put_calls == 0


@pytest.mark.parametrize(
    ("target", "reason"),
    (("replica", "replica_put_failed"), ("canonical", "canonical_put_failed")),
)
async def test_target_failure_is_fail_closed_and_persists_retryable_attempt(
    uow_factory: UnitOfWork,
    tmp_path: Path,
    target: str,
    reason: str,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False, fail_put=target == "replica")
    canonical = MemoryArtifactStore(canonical=True, fail_put=target == "canonical")

    with pytest.raises(ArtifactPublicationError, match=reason):
        await _publisher(uow_factory, replica, canonical).publish(
            _request(_bundle(tmp_path / "bundle"))
        )

    assert await _counts(uow_factory) == (0, 1, [reason])


@pytest.mark.parametrize(
    ("store", "reason"),
    (
        ("corrupt_read", "replica_verification_failed"),
        ("omit_version", "canonical_verification_failed"),
    ),
)
async def test_readback_or_canonical_version_failure_never_catalogs_success(
    uow_factory: UnitOfWork,
    tmp_path: Path,
    store: str,
    reason: str,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False, corrupt_read=store == "corrupt_read")
    canonical = MemoryArtifactStore(canonical=True, omit_version=store == "omit_version")

    with pytest.raises(ArtifactPublicationError, match=reason):
        await _publisher(uow_factory, replica, canonical).publish(
            _request(_bundle(tmp_path / "bundle"))
        )

    assert await _counts(uow_factory) == (0, 1, [reason])


async def test_catalog_failure_is_recorded_without_claiming_success(
    uow_factory: UnitOfWork,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)

    async def fail_catalog(self: ArtifactRepository, record: object) -> object:
        del self, record
        raise RuntimeError("database details must not escape")

    monkeypatch.setattr(ArtifactRepository, "record_publication", fail_catalog)
    with pytest.raises(ArtifactPublicationError, match="catalog_write_failed"):
        await _publisher(uow_factory, replica, canonical).publish(
            _request(_bundle(tmp_path / "bundle"))
        )

    assert await _counts(uow_factory) == (0, 1, ["catalog_write_failed"])


async def test_identical_retry_reuses_verified_objects_and_catalog_record(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)
    publisher = _publisher(uow_factory, replica, canonical)
    request = _request(_bundle(tmp_path / "bundle"))

    first = await publisher.publish(request)
    second = await publisher.publish(request)

    assert second == first
    assert await _counts(uow_factory) == (1, 1, [])
    assert replica.put_calls == canonical.put_calls == 4


async def test_failed_attempt_can_retry_with_same_immutable_keys(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False, fail_put=True)
    canonical = MemoryArtifactStore(canonical=True)
    publisher = _publisher(uow_factory, replica, canonical)
    request = _request(_bundle(tmp_path / "bundle"))

    with pytest.raises(ArtifactPublicationError, match="replica_put_failed"):
        await publisher.publish(request)
    replica.fail_put = False
    record = await publisher.publish(request)

    assert record.report_id == REPORT_ID
    assert await _counts(uow_factory) == (1, 2, ["replica_put_failed"])


async def test_conflicting_retry_is_rejected_before_overwriting_objects(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)
    publisher = _publisher(uow_factory, replica, canonical)
    await publisher.publish(_request(_bundle(tmp_path / "first")))

    with pytest.raises(ArtifactPublicationError, match="publication_conflict"):
        await publisher.publish(_request(_bundle(tmp_path / "second", status="changed")))

    assert await _counts(uow_factory) == (1, 1, [])
    assert replica.put_calls == canonical.put_calls == 2


async def test_existing_record_from_different_operation_identity_is_not_reused(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False)
    canonical = MemoryArtifactStore(canonical=True)
    publisher = _publisher(uow_factory, replica, canonical)
    request = _request(_bundle(tmp_path / "bundle"))
    await publisher.publish(request)

    with pytest.raises(ArtifactPublicationError, match="publication_conflict"):
        await publisher.publish(replace(request, producing_service_id="operations-2"))

    assert await _counts(uow_factory) == (1, 1, [])
    assert replica.put_calls == canonical.put_calls == 2


async def test_failure_recording_failure_keeps_original_nonzero_outcome(
    uow_factory: UnitOfWork,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _prepare(uow_factory)
    replica = MemoryArtifactStore(canonical=False, fail_put=True)
    canonical = MemoryArtifactStore(canonical=True)

    async def fail_persistence(
        self: ArtifactRepository,
        attempt_id: UUID,
        *,
        reason_code: str,
        failed_at: datetime,
    ) -> object:
        del self, attempt_id, reason_code, failed_at
        raise RuntimeError("persistence failed")

    monkeypatch.setattr(ArtifactRepository, "fail_attempt", fail_persistence)
    with pytest.raises(ArtifactPublicationError, match="replica_put_failed"):
        await _publisher(uow_factory, replica, canonical).publish(
            _request(_bundle(tmp_path / "bundle"))
        )

    assert "artifact_publication_original_failure" in caplog.text
    assert "artifact_publication_failure_persistence_failed" in caplog.text
