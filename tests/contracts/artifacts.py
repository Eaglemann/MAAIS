from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maais.artifacts.models import (
    ArtifactPutDisposition,
    ArtifactWriteRequest,
    RetentionRequest,
)
from maais.artifacts.store import ArtifactCollisionError, ArtifactStore, MissingArtifactError
from maais.config.artifacts import RetentionMode


class ArtifactStoreContract:
    """Reusable behavioral contract inherited by every concrete store test."""

    @pytest.fixture
    def artifact_store(self, tmp_path: Path) -> ArtifactStore:
        raise NotImplementedError

    @pytest.fixture
    def artifact_request(self, tmp_path: Path) -> ArtifactWriteRequest:
        source = tmp_path / "source.json"
        source.write_bytes(b'{"status":"ready"}\n')
        content = source.read_bytes()
        return ArtifactWriteRequest(
            key="maais/qualification/candidate/run/daily_report/report-001/report.json",
            source_path=source,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type="application/json",
            retention=RetentionRequest(
                mode=RetentionMode.COMPLIANCE,
                retain_until=datetime(2027, 8, 8, tzinfo=timezone.utc),
            ),
        )

    @pytest.mark.asyncio
    async def test_new_put_and_identical_retry_are_idempotent(
        self,
        artifact_store: ArtifactStore,
        artifact_request: ArtifactWriteRequest,
    ) -> None:
        created = await artifact_store.put_verified(artifact_request)
        retried = await artifact_store.put_verified(artifact_request)

        assert created.disposition is ArtifactPutDisposition.CREATED
        assert retried.disposition is ArtifactPutDisposition.IDENTICAL_RETRY
        assert retried.artifact == created.artifact

    @pytest.mark.asyncio
    async def test_collision_is_rejected(
        self,
        artifact_store: ArtifactStore,
        artifact_request: ArtifactWriteRequest,
        tmp_path: Path,
    ) -> None:
        await artifact_store.put_verified(artifact_request)
        conflicting_source = tmp_path / "conflict.json"
        conflicting_source.write_bytes(b"different")
        conflicting = ArtifactWriteRequest(
            key=artifact_request.key,
            source_path=conflicting_source,
            sha256=hashlib.sha256(b"different").hexdigest(),
            size_bytes=len(b"different"),
            content_type=artifact_request.content_type,
            retention=artifact_request.retention,
        )

        with pytest.raises(ArtifactCollisionError):
            await artifact_store.put_verified(conflicting)

    @pytest.mark.asyncio
    async def test_head_and_exact_version_chunked_read(
        self,
        artifact_store: ArtifactStore,
        artifact_request: ArtifactWriteRequest,
    ) -> None:
        result = await artifact_store.put_verified(artifact_request)

        metadata = await artifact_store.head(
            artifact_request.key,
            version_id=result.artifact.version_id,
        )
        content = b"".join(
            [
                chunk
                async for chunk in artifact_store.read_chunks(
                    artifact_request.key,
                    version_id=result.artifact.version_id,
                )
            ]
        )

        assert metadata == result.artifact
        assert hashlib.sha256(content).hexdigest() == artifact_request.sha256

    @pytest.mark.asyncio
    async def test_missing_object_fails_closed(
        self,
        artifact_store: ArtifactStore,
    ) -> None:
        with pytest.raises(MissingArtifactError):
            await artifact_store.head("maais/qualification/missing.json")

    @pytest.mark.asyncio
    async def test_store_reports_capabilities(
        self,
        artifact_store: ArtifactStore,
    ) -> None:
        capabilities = await artifact_store.capabilities()

        assert capabilities.store_name
        assert capabilities.immutable_create is True
        assert capabilities.exact_version_reads is True
