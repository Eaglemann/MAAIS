from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import UUID

from maais.artifacts.bundles import validate_bundle
from maais.artifacts.models import ArtifactRecord, ArtifactType, validate_sha256
from maais.artifacts.publisher import PublicationRequest
from maais.operations.backups import BackupBundlePaths
from maais.operations.reporting import ReportBundlePaths
from maais.operations.restores import load_verified_backup

UTC = timezone.utc


class ArtifactPublisherPort(Protocol):
    async def publish(self, request: PublicationRequest) -> ArtifactRecord: ...


@dataclass(frozen=True, slots=True)
class CloudArtifactIdentity:
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    operation_id: UUID
    generated_at: datetime
    producing_deployment_id: str
    producing_service_id: str

    def __post_init__(self) -> None:
        if self.environment not in {"qualification", "production"}:
            raise ValueError("cloud artifact environment must be qualification or production")
        validate_sha256(self.candidate_hash)
        for name, value in (
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
            ("operation_id", self.operation_id),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"cloud artifact {name} must be a non-nil UUID")
        if self.generated_at.tzinfo is not UTC:
            raise ValueError("cloud artifact generated_at must use the UTC timezone")
        for name, value in (
            ("producing_deployment_id", self.producing_deployment_id),
            ("producing_service_id", self.producing_service_id),
        ):
            if not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"cloud artifact {name} must be nonempty and bounded")


async def publish_verified_bundle(
    publisher: ArtifactPublisherPort,
    directory: Path,
    *,
    artifact_type: ArtifactType,
    report_id: str,
    identity: CloudArtifactIdentity,
) -> ArtifactRecord:
    """Reject local evidence corruption before invoking any remote storage adapter."""
    await asyncio.to_thread(
        validate_bundle,
        directory,
        expected_report_id=report_id,
    )
    return await publisher.publish(
        PublicationRequest(
            bundle_directory=directory,
            environment=identity.environment,
            candidate_hash=identity.candidate_hash,
            experiment_id=identity.experiment_id,
            run_id=identity.run_id,
            operation_id=identity.operation_id,
            artifact_type=artifact_type,
            report_id=report_id,
            generated_at=identity.generated_at,
            producing_deployment_id=identity.producing_deployment_id,
            producing_service_id=identity.producing_service_id,
        )
    )


async def publish_daily_report_bundle(
    publisher: ArtifactPublisherPort,
    paths: ReportBundlePaths,
    *,
    report_id: str,
    identity: CloudArtifactIdentity,
) -> ArtifactRecord:
    return await publish_verified_bundle(
        publisher,
        paths.directory,
        artifact_type=ArtifactType.DAILY_REPORT,
        report_id=report_id,
        identity=identity,
    )


async def publish_logical_backup_bundle(
    publisher: ArtifactPublisherPort,
    paths: BackupBundlePaths,
    *,
    identity: CloudArtifactIdentity,
) -> ArtifactRecord:
    verified = await asyncio.to_thread(load_verified_backup, paths.directory)
    if verified.report_id is None or paths.report_id != verified.report_id:
        raise ValueError("cloud backup bundle is missing its immutable report identity")
    producer = verified.producer
    if producer is None or not (
        producer.environment == identity.environment
        and producer.candidate_hash == identity.candidate_hash
        and producer.experiment_id == identity.experiment_id
        and producer.run_id == identity.run_id
        and producer.operation_id == identity.operation_id
        and producer.railway_deployment_id == identity.producing_deployment_id
    ):
        raise ValueError("cloud backup producer identity differs from publication authority")
    return await publish_verified_bundle(
        publisher,
        paths.directory,
        artifact_type=ArtifactType.LOGICAL_BACKUP,
        report_id=verified.report_id,
        identity=identity,
    )
