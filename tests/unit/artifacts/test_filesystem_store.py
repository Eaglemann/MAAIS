from __future__ import annotations

import asyncio
import hashlib
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maais.artifacts.filesystem import FilesystemArtifactStore
from maais.artifacts.models import (
    ArtifactPutDisposition,
    ArtifactWriteRequest,
    RetentionRequest,
)
from maais.artifacts.store import ArtifactVerificationError
from maais.config.artifacts import RetentionMode


def _request(
    tmp_path: Path, *, key: str = "maais/qualification/report.json"
) -> ArtifactWriteRequest:
    source = tmp_path / "source.json"
    source.write_bytes(b'{"status":"ready"}\n')
    content = source.read_bytes()
    return ArtifactWriteRequest(
        key=key,
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
async def test_filesystem_store_creates_owner_only_immutable_object(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = FilesystemArtifactStore(root=root)
    request = _request(tmp_path)

    result = await store.put_verified(request)

    target = root / request.key
    assert result.disposition is ArtifactPutDisposition.CREATED
    assert target.read_bytes() == request.source_path.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_concurrent_identical_puts_create_once_and_retry_once(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(root=tmp_path / "store")
    request = _request(tmp_path)

    results = await asyncio.gather(
        store.put_verified(request),
        store.put_verified(request),
    )

    assert {result.disposition for result in results} == {
        ArtifactPutDisposition.CREATED,
        ArtifactPutDisposition.IDENTICAL_RETRY,
    }
    assert results[0].artifact == results[1].artifact


def test_filesystem_store_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        FilesystemArtifactStore(root=linked)


@pytest.mark.asyncio
async def test_filesystem_store_rejects_symlink_source_and_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    store = FilesystemArtifactStore(root=root)
    request = _request(tmp_path)
    linked_source = tmp_path / "linked-source.json"
    linked_source.symlink_to(request.source_path)
    linked_request = ArtifactWriteRequest(
        key=request.key,
        source_path=linked_source,
        sha256=request.sha256,
        size_bytes=request.size_bytes,
        content_type=request.content_type,
        retention=request.retention,
    )

    with pytest.raises(ArtifactVerificationError, match="symlink"):
        await store.put_verified(linked_request)

    destination_parent = root / "maais"
    destination_parent.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactVerificationError, match="symlink"):
        await store.put_verified(request)


@pytest.mark.asyncio
async def test_filesystem_store_recomputes_source_digest_before_commit(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(root=tmp_path / "store")
    request = _request(tmp_path)
    request.source_path.write_bytes(b"mutated")

    with pytest.raises(ArtifactVerificationError, match="SHA-256|size"):
        await store.put_verified(request)

    assert not (tmp_path / "store" / request.key).exists()


@pytest.mark.asyncio
async def test_identical_retry_repairs_content_fsynced_before_metadata_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    store = FilesystemArtifactStore(root=root)
    request = _request(tmp_path)
    orphan = root / request.key
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(request.source_path.read_bytes())
    orphan.chmod(0o600)

    result = await store.put_verified(request)

    assert result.disposition is ArtifactPutDisposition.IDENTICAL_RETRY
    assert await store.head(request.key) == result.artifact
