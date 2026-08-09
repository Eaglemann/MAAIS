from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maais.artifacts.models import (
    DEFAULT_HASH_CHUNK_SIZE,
    ArtifactPutDisposition,
    ArtifactPutResult,
    ArtifactWriteRequest,
    RetentionRequest,
    StoreCapabilities,
    StoredArtifact,
    validate_object_key,
)
from maais.artifacts.store import (
    ArtifactCollisionError,
    ArtifactStoreError,
    ArtifactVerificationError,
    MissingArtifactError,
)
from maais.artifacts.verification import hash_file
from maais.config.artifacts import RetentionMode


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


class FilesystemArtifactStore:
    def __init__(self, *, root: Path, store_name: str = "filesystem") -> None:
        if not store_name.strip():
            raise ValueError("artifact store name must not be empty")
        if root.is_symlink():
            raise ValueError("artifact store root must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("artifact store root must be a non-symlink directory")
        os.chmod(root, 0o700)
        self._root = root.resolve(strict=True)
        self._metadata_root = self._root / ".maais-metadata"
        self._metadata_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self._metadata_root, 0o700)
        self._store_name = store_name
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def capabilities(self) -> StoreCapabilities:
        return StoreCapabilities(
            store_name=self._store_name,
            immutable_create=True,
            exact_version_reads=True,
            versioning_enabled=False,
            object_lock_enabled=False,
            compliance_retention_supported=False,
        )

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def put_verified(self, request: ArtifactWriteRequest) -> ArtifactPutResult:
        actual = await asyncio.to_thread(hash_file, request.source_path)
        if actual.size_bytes != request.size_bytes:
            raise ArtifactVerificationError(
                "artifact source size mismatch before filesystem commit"
            )
        if actual.sha256 != request.sha256:
            raise ArtifactVerificationError(
                "artifact source SHA-256 mismatch before filesystem commit"
            )
        key_lock = await self._lock_for(request.key)
        async with key_lock:
            return await asyncio.to_thread(self._put_blocking, request)

    async def head(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> StoredArtifact:
        return await asyncio.to_thread(self._head_blocking, key, version_id)

    async def read_chunks(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        metadata = await self.head(key, version_id=version_id)
        target = self._object_path(key, create_parents=False)
        descriptor = await asyncio.to_thread(self._open_readonly, target)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            while chunk := await asyncio.to_thread(os.read, descriptor, DEFAULT_HASH_CHUNK_SIZE):
                digest.update(chunk)
                size_bytes += len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(os.close, descriptor)
        if size_bytes != metadata.size_bytes or digest.hexdigest() != metadata.sha256:
            raise ArtifactVerificationError("filesystem artifact read-back verification failed")

    def _put_blocking(self, request: ArtifactWriteRequest) -> ArtifactPutResult:
        target = self._object_path(request.key, create_parents=True)
        if target.exists() or target.is_symlink():
            return ArtifactPutResult(
                artifact=self._verify_existing(request),
                disposition=ArtifactPutDisposition.IDENTICAL_RETRY,
            )

        created = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            created = True
            try:
                actual_sha256, actual_size = self._copy_and_hash(request.source_path, descriptor)
                if actual_size != request.size_bytes:
                    raise ArtifactVerificationError(
                        "artifact source size mismatch before filesystem commit"
                    )
                if actual_sha256 != request.sha256:
                    raise ArtifactVerificationError(
                        "artifact source SHA-256 mismatch before filesystem commit"
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(target, 0o600)
            self._fsync_directory(target.parent)

            stored = StoredArtifact(
                store_name=self._store_name,
                key=request.key,
                etag=request.sha256,
                version_id=request.sha256,
                sha256=request.sha256,
                size_bytes=request.size_bytes,
                content_type=request.content_type,
                retention=request.retention,
                stored_at=datetime.now(timezone.utc),
            )
            self._write_metadata(stored)
            return ArtifactPutResult(
                artifact=stored,
                disposition=ArtifactPutDisposition.CREATED,
            )
        except FileExistsError:
            try:
                existing = self._verify_existing(request)
            except ArtifactStoreError:
                if created:
                    target.unlink(missing_ok=True)
                    self._fsync_directory(target.parent)
                raise
            return ArtifactPutResult(
                artifact=existing,
                disposition=ArtifactPutDisposition.IDENTICAL_RETRY,
            )
        except (ArtifactStoreError, OSError):
            if created:
                target.unlink(missing_ok=True)
                self._fsync_directory(target.parent)
            raise

    def _verify_existing(self, request: ArtifactWriteRequest) -> StoredArtifact:
        try:
            stored = self._read_metadata(request.key)
        except MissingArtifactError as error:
            target = self._object_path(request.key, create_parents=False)
            actual_sha256, actual_size = self._hash_open_file(target)
            if actual_sha256 != request.sha256 or actual_size != request.size_bytes:
                raise ArtifactCollisionError(
                    "filesystem artifact key exists without matching committed metadata"
                ) from error
            stored = StoredArtifact(
                store_name=self._store_name,
                key=request.key,
                etag=request.sha256,
                version_id=request.sha256,
                sha256=request.sha256,
                size_bytes=request.size_bytes,
                content_type=request.content_type,
                retention=request.retention,
                stored_at=datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc),
            )
            try:
                self._write_metadata(stored)
            except FileExistsError:
                stored = self._read_metadata(request.key)
        if (
            stored.sha256 != request.sha256
            or stored.size_bytes != request.size_bytes
            or stored.content_type != request.content_type
            or stored.retention != request.retention
        ):
            raise ArtifactCollisionError("filesystem artifact key collision")
        target = self._object_path(request.key, create_parents=False)
        actual_sha256, actual_size = self._hash_open_file(target)
        if actual_sha256 != request.sha256 or actual_size != request.size_bytes:
            raise ArtifactCollisionError(
                "filesystem artifact bytes conflict with cataloged metadata"
            )
        return stored

    def _head_blocking(self, key: str, version_id: str | None) -> StoredArtifact:
        stored = self._read_metadata(key)
        if version_id is not None and version_id != stored.version_id:
            raise MissingArtifactError("filesystem artifact version does not exist")
        target = self._object_path(key, create_parents=False)
        if target.is_symlink() or not target.is_file():
            raise MissingArtifactError("filesystem artifact object does not exist")
        if target.stat().st_size != stored.size_bytes:
            raise ArtifactVerificationError("filesystem artifact size differs from metadata")
        return stored

    def _object_path(self, key: str, *, create_parents: bool) -> Path:
        validate_object_key(key)
        parts = key.split("/")
        parent = self._safe_parent(self._root, parts[:-1], create=create_parents)
        target = parent / parts[-1]
        if target.is_symlink():
            raise ArtifactVerificationError("artifact destination must not be a symlink")
        return target

    def _metadata_path(self, key: str, *, create_parents: bool) -> Path:
        parts = validate_object_key(key).split("/")
        parent = self._safe_parent(self._metadata_root, parts[:-1], create=create_parents)
        target = parent / f"{parts[-1]}.metadata.json"
        if target.is_symlink():
            raise ArtifactVerificationError("artifact metadata must not be a symlink")
        return target

    @staticmethod
    def _safe_parent(root: Path, parts: list[str], *, create: bool) -> Path:
        current = root
        for index, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                raise ArtifactVerificationError("artifact path must not traverse a symlink")
            if not current.exists() and not create:
                return current.joinpath(*parts[index + 1 :])
            if create and not current.exists():
                current.mkdir(mode=0o700, exist_ok=True)
                os.chmod(current, 0o700)
            if current.is_symlink():
                raise ArtifactVerificationError("artifact path must not traverse a symlink")
            if not current.is_dir():
                raise ArtifactVerificationError("artifact parent must be a directory")
        try:
            current.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as error:
            raise ArtifactVerificationError("artifact path escapes its store root") from error
        return current

    @staticmethod
    def _open_readonly(path: Path) -> int:
        if path.is_symlink():
            raise ArtifactVerificationError("artifact source must not be a symlink")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise MissingArtifactError("filesystem artifact object does not exist") from error
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise ArtifactVerificationError("artifact source must be a regular file")
        return descriptor

    def _copy_and_hash(self, source_path: Path, destination: int) -> tuple[str, int]:
        source = self._open_readonly(source_path)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            while chunk := os.read(source, DEFAULT_HASH_CHUNK_SIZE):
                digest.update(chunk)
                size_bytes += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination, view)
                    view = view[written:]
        finally:
            os.close(source)
        return digest.hexdigest(), size_bytes

    def _hash_open_file(self, path: Path) -> tuple[str, int]:
        source = self._open_readonly(path)
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            while chunk := os.read(source, DEFAULT_HASH_CHUNK_SIZE):
                digest.update(chunk)
                size_bytes += len(chunk)
        finally:
            os.close(source)
        return digest.hexdigest(), size_bytes

    def _write_metadata(self, stored: StoredArtifact) -> None:
        target = self._metadata_path(stored.key, create_parents=True)
        payload = json.dumps(
            {
                "content_type": stored.content_type,
                "etag": stored.etag,
                "key": stored.key,
                "retention": {
                    "mode": stored.retention.mode.value,
                    "retain_until": _utc_iso(stored.retention.retain_until),
                },
                "sha256": stored.sha256,
                "size_bytes": stored.size_bytes,
                "store_name": stored.store_name,
                "stored_at": _utc_iso(stored.stored_at),
                "version_id": stored.version_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(target, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(target, 0o600)
        self._fsync_directory(target.parent)

    def _read_metadata(self, key: str) -> StoredArtifact:
        target = self._metadata_path(key, create_parents=False)
        try:
            payload: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
            retention = payload["retention"]
            if not isinstance(retention, dict):
                raise TypeError
            stored = StoredArtifact(
                store_name=str(payload["store_name"]),
                key=str(payload["key"]),
                etag=str(payload["etag"]),
                version_id=(
                    str(payload["version_id"]) if payload["version_id"] is not None else None
                ),
                sha256=str(payload["sha256"]),
                size_bytes=int(payload["size_bytes"]),
                content_type=str(payload["content_type"]),
                retention=RetentionRequest(
                    mode=RetentionMode(str(retention["mode"])),
                    retain_until=_parse_utc(str(retention["retain_until"])),
                ),
                stored_at=_parse_utc(str(payload["stored_at"])),
            )
        except FileNotFoundError as error:
            raise MissingArtifactError("filesystem artifact metadata does not exist") from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ArtifactVerificationError("filesystem artifact metadata is malformed") from error
        if stored.key != key or stored.store_name != self._store_name:
            raise ArtifactVerificationError("filesystem artifact metadata identity mismatch")
        return stored

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
