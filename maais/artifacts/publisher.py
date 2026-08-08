from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from maais.artifacts.bundles import (
    VerifiedArtifactBundle,
    stage_verified_bundle,
    validate_bundle,
)
from maais.artifacts.models import (
    ArtifactRecord,
    ArtifactWriteRequest,
    RetentionRequest,
    StoreCapabilities,
    StoredArtifact,
    artifact_key,
    validate_sha256,
)
from maais.artifacts.store import (
    ArtifactStore,
    ArtifactStoreError,
    ArtifactVerificationError,
    StoreCapabilityError,
)
from maais.config.artifacts import ARTIFACT_RETENTION_POLICIES, ArtifactType
from maais.db.repositories.artifacts import ArtifactCatalogConflict
from maais.db.unit_of_work import UnitOfWork

LOGGER = logging.getLogger(__name__)
UTC = timezone.utc


class ArtifactPublicationError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    bundle_directory: Path
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    operation_id: UUID
    artifact_type: ArtifactType
    report_id: str
    generated_at: datetime
    producing_deployment_id: str
    producing_service_id: str

    def __post_init__(self) -> None:
        if self.environment not in {"qualification", "production"}:
            raise ValueError("publication environment must be qualification or production")
        validate_sha256(self.candidate_hash)
        validate_sha256(self.report_id)
        for name, value in (
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
            ("operation_id", self.operation_id),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"publication {name} must be a non-nil UUID")
        if not isinstance(self.artifact_type, ArtifactType):
            raise ValueError("publication artifact_type must be an ArtifactType")
        _require_utc(self.generated_at, "generated_at")
        for name, value in (
            ("producing_deployment_id", self.producing_deployment_id),
            ("producing_service_id", self.producing_service_id),
        ):
            if not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"publication {name} must be nonempty, trimmed, and bounded")


class ArtifactPublisher:
    def __init__(
        self,
        *,
        replica: ArtifactStore,
        canonical: ArtifactStore,
        uow_factory: UnitOfWork,
        now: Callable[[], datetime] | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if replica is canonical:
            raise ValueError("artifact publisher targets must be independent instances")
        self._replica = replica
        self._canonical = canonical
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._uuid_factory = uuid_factory

    async def publish(self, request: PublicationRequest) -> ArtifactRecord:
        try:
            source = await asyncio.to_thread(
                validate_bundle,
                request.bundle_directory,
                expected_report_id=request.report_id,
            )
        except (ArtifactStoreError, OSError, ValueError) as error:
            raise ArtifactPublicationError("bundle_invalid") from error

        existing = await self._find_existing(request)
        if existing is not None:
            if (
                existing.bundle_content_hash != source.content_hash
                or not self._existing_identity_matches(existing, request)
            ):
                raise ArtifactPublicationError("publication_conflict")

        manager = stage_verified_bundle(source)
        try:
            staged = await asyncio.to_thread(manager.__enter__)
        except (ArtifactStoreError, OSError, ValueError) as error:
            raise ArtifactPublicationError("bundle_staging_failed") from error
        publication_exception: BaseException | None = None
        try:
            if existing is not None:
                return await self._verify_identical_retry(request, staged, existing)
            return await self._publish_new(request, staged)
        except BaseException as error:
            publication_exception = error
            raise
        finally:
            try:
                await asyncio.to_thread(manager.__exit__, None, None, None)
            except Exception as cleanup_error:
                if publication_exception is None:
                    raise ArtifactPublicationError("bundle_cleanup_failed") from cleanup_error
                LOGGER.error(
                    "artifact_publication_bundle_cleanup_failed",
                    extra={
                        "cleanup_exception_type": type(cleanup_error).__name__,
                        "publication_exception_type": type(publication_exception).__name__,
                    },
                    exc_info=(
                        type(cleanup_error),
                        cleanup_error,
                        cleanup_error.__traceback__,
                    ),
                )

    async def _publish_new(
        self,
        request: PublicationRequest,
        bundle: VerifiedArtifactBundle,
    ) -> ArtifactRecord:
        started_at = self._timestamp("publication start")
        try:
            async with self._uow_factory.begin() as uow:
                attempt = await uow.artifacts.begin_attempt(
                    attempt_id=self._uuid_factory(),
                    operation_id=request.operation_id,
                    bundle_content_hash=bundle.content_hash,
                    started_at=started_at,
                )
        except Exception as error:
            raise ArtifactPublicationError("attempt_start_failed") from error

        original: BaseException | None = None
        try:
            replica_capabilities, canonical_capabilities = await self._capabilities()
            retention = self._retention(request)
            replica = await self._publish_store(
                self._replica,
                replica_capabilities,
                bundle,
                request,
                retention,
                target="replica",
            )
            canonical = await self._publish_store(
                self._canonical,
                canonical_capabilities,
                bundle,
                request,
                retention,
                target="canonical",
            )
            try:
                return await self._catalog(
                    request=request,
                    bundle=bundle,
                    publication_attempt_id=attempt.id,
                    replica=replica,
                    canonical=canonical,
                )
            except ArtifactPublicationError:
                raise
            except Exception as error:
                raise ArtifactPublicationError("catalog_write_failed") from error
        except ArtifactPublicationError as error:
            original = error
            publication_error = error
        except Exception as error:  # pragma: no cover - defensive boundary
            original = error
            publication_error = ArtifactPublicationError("publication_failed")

        try:
            async with self._uow_factory.begin() as uow:
                await uow.artifacts.fail_attempt(
                    attempt.id,
                    reason_code=publication_error.reason_code,
                    failed_at=self._timestamp("publication failure"),
                )
        except Exception as persistence_error:
            if original is not None:
                LOGGER.error(
                    "artifact_publication_original_failure",
                    extra={
                        "operation_id": str(request.operation_id),
                        "reason_code": publication_error.reason_code,
                        "publication_exception_type": type(original).__name__,
                    },
                    exc_info=(type(original), original, original.__traceback__),
                )
            LOGGER.error(
                "artifact_publication_failure_persistence_failed",
                extra={
                    "operation_id": str(request.operation_id),
                    "reason_code": publication_error.reason_code,
                    "persistence_exception_type": type(persistence_error).__name__,
                    "publication_exception_type": type(original).__name__,
                },
                exc_info=(
                    type(persistence_error),
                    persistence_error,
                    persistence_error.__traceback__,
                ),
            )
        raise publication_error from original

    async def _verify_identical_retry(
        self,
        request: PublicationRequest,
        bundle: VerifiedArtifactBundle,
        existing: ArtifactRecord,
    ) -> ArtifactRecord:
        replica_capabilities, canonical_capabilities = await self._capabilities()
        retention = self._retention(request)
        try:
            replica = await self._publish_store(
                self._replica,
                replica_capabilities,
                bundle,
                request,
                retention,
                target="replica",
            )
            canonical = await self._publish_store(
                self._canonical,
                canonical_capabilities,
                bundle,
                request,
                retention,
                target="canonical",
            )
        except ArtifactPublicationError:
            raise
        if replica != existing.replica_inventory or canonical != existing.canonical_inventory:
            raise ArtifactPublicationError("publication_conflict")
        return existing

    async def _capabilities(self) -> tuple[StoreCapabilities, StoreCapabilities]:
        try:
            replica = await self._replica.capabilities()
        except Exception as error:
            raise ArtifactPublicationError("replica_capability_failed") from error
        try:
            canonical = await self._canonical.capabilities()
        except Exception as error:
            raise ArtifactPublicationError("canonical_capability_failed") from error
        if replica.store_name == canonical.store_name or not replica.immutable_create:
            raise ArtifactPublicationError("replica_capability_failed")
        if not (
            canonical.immutable_create
            and canonical.exact_version_reads
            and canonical.versioning_enabled
            and canonical.object_lock_enabled
            and canonical.compliance_retention_supported
        ):
            raise ArtifactPublicationError("canonical_capability_failed")
        return replica, canonical

    async def _publish_store(
        self,
        store: ArtifactStore,
        capabilities: StoreCapabilities,
        bundle: VerifiedArtifactBundle,
        publication: PublicationRequest,
        retention: RetentionRequest,
        *,
        target: str,
    ) -> tuple[StoredArtifact, ...]:
        inventory: list[StoredArtifact] = []
        for source in bundle.files:
            request = ArtifactWriteRequest.from_verified_file(
                key=artifact_key(
                    environment=publication.environment,
                    candidate_hash=publication.candidate_hash,
                    experiment_id=publication.experiment_id,
                    artifact_type=publication.artifact_type.value,
                    report_id=publication.report_id,
                    relative_path=source.relative_path,
                ),
                source=source,
                retention=retention,
            )
            try:
                result = await store.put_verified(request)
            except (ArtifactVerificationError, StoreCapabilityError) as error:
                raise ArtifactPublicationError(f"{target}_verification_failed") from error
            except ArtifactStoreError as error:
                raise ArtifactPublicationError(f"{target}_put_failed") from error
            except Exception as error:  # pragma: no cover - adapter boundary
                raise ArtifactPublicationError(f"{target}_put_failed") from error
            try:
                self._verify_metadata(
                    request,
                    result.artifact,
                    capabilities=capabilities,
                    canonical=target == "canonical",
                )
                exact_version = (
                    result.artifact.version_id if capabilities.exact_version_reads else None
                )
                stored = await store.head(request.key, version_id=exact_version)
                if exact_version is not None and stored.version_id != exact_version:
                    raise ArtifactVerificationError("published artifact exact version differs")
                self._verify_metadata(
                    request,
                    stored,
                    capabilities=capabilities,
                    canonical=target == "canonical",
                )
                digest = hashlib.sha256()
                size_bytes = 0
                async for chunk in store.read_chunks(request.key, version_id=exact_version):
                    digest.update(chunk)
                    size_bytes += len(chunk)
                if digest.hexdigest() != request.sha256 or size_bytes != request.size_bytes:
                    raise ArtifactVerificationError("published artifact read-back differs")
            except Exception as error:
                raise ArtifactPublicationError(f"{target}_verification_failed") from error
            inventory.append(stored)
        return tuple(inventory)

    @staticmethod
    def _verify_metadata(
        request: ArtifactWriteRequest,
        stored: StoredArtifact,
        *,
        capabilities: StoreCapabilities,
        canonical: bool,
    ) -> None:
        if (
            stored.store_name != capabilities.store_name
            or stored.key != request.key
            or stored.sha256 != request.sha256
            or stored.size_bytes != request.size_bytes
            or stored.content_type != request.content_type
            or stored.retention != request.retention
        ):
            raise ArtifactVerificationError("published artifact metadata differs")
        if canonical and not stored.version_id:
            raise ArtifactVerificationError("canonical artifact version is missing")

    async def _catalog(
        self,
        *,
        request: PublicationRequest,
        bundle: VerifiedArtifactBundle,
        publication_attempt_id: UUID,
        replica: tuple[StoredArtifact, ...],
        canonical: tuple[StoredArtifact, ...],
    ) -> ArtifactRecord:
        recorded_at = self._timestamp("catalog recording")
        try:
            async with self._uow_factory.begin() as uow:
                sequence, previous_hash = await uow.artifacts.next_stream_position(
                    environment=request.environment,
                    candidate_hash=request.candidate_hash,
                    experiment_id=request.experiment_id,
                )
                record = ArtifactRecord.create(
                    record_id=self._uuid_factory(),
                    operation_id=request.operation_id,
                    publication_attempt_id=publication_attempt_id,
                    environment=request.environment,
                    candidate_hash=request.candidate_hash,
                    experiment_id=request.experiment_id,
                    run_id=request.run_id,
                    artifact_type=request.artifact_type,
                    report_id=request.report_id,
                    bundle_content_hash=bundle.content_hash,
                    size_bytes=bundle.size_bytes,
                    media_type=bundle.media_type,
                    generated_at=request.generated_at,
                    recorded_at=recorded_at,
                    producing_deployment_id=request.producing_deployment_id,
                    producing_service_id=request.producing_service_id,
                    sequence=sequence,
                    replica_inventory=replica,
                    canonical_inventory=canonical,
                    previous_evidence_hash=previous_hash,
                )
                return await uow.artifacts.record_publication(record)
        except ArtifactCatalogConflict as error:
            raise ArtifactPublicationError("catalog_write_failed") from error

    async def _find_existing(self, request: PublicationRequest) -> ArtifactRecord | None:
        try:
            async with self._uow_factory.begin() as uow:
                return await uow.artifacts.find_report(
                    environment=request.environment,
                    candidate_hash=request.candidate_hash,
                    experiment_id=request.experiment_id,
                    artifact_type=request.artifact_type,
                    report_id=request.report_id,
                )
        except Exception as error:
            raise ArtifactPublicationError("catalog_read_failed") from error

    @staticmethod
    def _existing_identity_matches(
        existing: ArtifactRecord,
        request: PublicationRequest,
    ) -> bool:
        return (
            existing.environment == request.environment
            and existing.candidate_hash == request.candidate_hash
            and existing.experiment_id == request.experiment_id
            and existing.run_id == request.run_id
            and existing.operation_id == request.operation_id
            and existing.artifact_type is request.artifact_type
            and existing.report_id == request.report_id
            and existing.generated_at == request.generated_at
            and existing.producing_deployment_id == request.producing_deployment_id
            and existing.producing_service_id == request.producing_service_id
        )

    @staticmethod
    def _retention(request: PublicationRequest) -> RetentionRequest:
        mode, days = ARTIFACT_RETENTION_POLICIES[request.artifact_type]
        return RetentionRequest(
            mode=mode,
            retain_until=request.generated_at + timedelta(days=days),
        )

    def _timestamp(self, label: str) -> datetime:
        value = self._now()
        _require_utc(value, label)
        return value


def _require_utc(value: datetime, label: str) -> None:
    if value.tzinfo is not UTC:
        raise ValueError(f"{label} must use the UTC timezone")
