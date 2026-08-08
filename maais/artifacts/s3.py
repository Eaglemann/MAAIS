from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Final

from botocore.exceptions import BotoCoreError, ClientError

from maais.artifacts.models import (
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
    StoreCapabilityError,
)
from maais.artifacts.verification import hash_file
from maais.config.artifacts import RetentionMode

MAX_S3_UPLOAD_CHUNK_SIZE: Final = 8 * 1024 * 1024
_OBJECT_EXISTS_CODES = frozenset({"ConditionalRequestConflict", "PreconditionFailed"})
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchVersion", "NotFound"})


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class _BoundedFileBody:
    """File-like request body that never returns an unbounded in-memory chunk."""

    def __init__(self, raw: BinaryIO, *, chunk_size: int = MAX_S3_UPLOAD_CHUNK_SIZE) -> None:
        if chunk_size <= 0:
            raise ValueError("S3 upload chunk size must be positive")
        self._raw = raw
        self._chunk_size = chunk_size

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        bounded_size = self._chunk_size if size < 0 else min(size, self._chunk_size)
        return self._raw.read(bounded_size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return self._raw.seekable()


class _ObjectAlreadyExists(Exception):
    pass


class S3ArtifactStore:
    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        canonical: bool,
        store_name: str | None = None,
        chunk_size: int = MAX_S3_UPLOAD_CHUNK_SIZE,
    ) -> None:
        if not bucket.strip() or bucket != bucket.strip():
            raise ValueError("S3 artifact bucket must be non-empty and trimmed")
        if chunk_size <= 0 or chunk_size > MAX_S3_UPLOAD_CHUNK_SIZE:
            raise ValueError("S3 upload chunks must be between 1 byte and 8 MiB")
        resolved_name = store_name or ("canonical" if canonical else "replica")
        if not resolved_name.strip():
            raise ValueError("artifact store name must not be empty")
        self._client = client
        self._bucket = bucket
        self._canonical = canonical
        self._store_name = resolved_name
        self._chunk_size = chunk_size

    async def capabilities(self) -> StoreCapabilities:
        if not self._canonical:
            return StoreCapabilities(
                store_name=self._store_name,
                immutable_create=True,
                exact_version_reads=False,
                versioning_enabled=False,
                object_lock_enabled=False,
                compliance_retention_supported=False,
            )
        try:
            versioning = await self._call("get_bucket_versioning", Bucket=self._bucket)
        except ArtifactStoreError as error:
            raise StoreCapabilityError(
                "canonical versioning capability could not be verified"
            ) from error
        if versioning.get("Status") != "Enabled":
            raise StoreCapabilityError("canonical S3 bucket versioning is not enabled")
        try:
            lock = await self._call("get_object_lock_configuration", Bucket=self._bucket)
        except ArtifactStoreError as error:
            raise StoreCapabilityError(
                "canonical Object Lock capability could not be verified"
            ) from error
        configuration = lock.get("ObjectLockConfiguration")
        if (
            not isinstance(configuration, dict)
            or configuration.get("ObjectLockEnabled") != "Enabled"
        ):
            raise StoreCapabilityError("canonical S3 Object Lock is not enabled")
        return StoreCapabilities(
            store_name=self._store_name,
            immutable_create=True,
            exact_version_reads=True,
            versioning_enabled=True,
            object_lock_enabled=True,
            compliance_retention_supported=True,
        )

    async def put_verified(self, request: ArtifactWriteRequest) -> ArtifactPutResult:
        actual = await asyncio.to_thread(hash_file, request.source_path)
        if actual.size_bytes != request.size_bytes:
            raise ArtifactVerificationError("artifact source size mismatch before S3 upload")
        if actual.sha256 != request.sha256:
            raise ArtifactVerificationError("artifact source SHA-256 mismatch before S3 upload")
        if self._canonical:
            await self.capabilities()

        try:
            response = await asyncio.to_thread(self._put_blocking, request)
        except _ObjectAlreadyExists:
            existing = await self.head(request.key)
            self._verify_metadata_matches(request, existing, collision=True)
            await self._verify_read_back(request, existing)
            return ArtifactPutResult(
                artifact=existing,
                disposition=ArtifactPutDisposition.IDENTICAL_RETRY,
            )

        raw_version_id = response.get("VersionId")
        version_id = str(raw_version_id) if raw_version_id is not None else None
        if self._canonical and not version_id:
            raise StoreCapabilityError("canonical S3 put did not return a version ID")
        stored = await self.head(request.key, version_id=version_id)
        self._verify_metadata_matches(request, stored, collision=False)
        await self._verify_read_back(request, stored)
        return ArtifactPutResult(
            artifact=stored,
            disposition=ArtifactPutDisposition.CREATED,
        )

    async def head(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> StoredArtifact:
        validate_object_key(key)
        parameters: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if version_id is not None:
            parameters["VersionId"] = version_id
        try:
            response = await self._call("head_object", **parameters)
        except MissingArtifactError:
            raise
        return self._stored_from_head(key, response, requested_version_id=version_id)

    async def read_chunks(
        self,
        key: str,
        *,
        version_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        validate_object_key(key)
        if self._canonical and not version_id:
            raise ArtifactVerificationError("canonical S3 reads require an exact version ID")
        parameters: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if version_id is not None:
            parameters["VersionId"] = version_id
        response = await self._call("get_object", **parameters)
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise ArtifactVerificationError("S3 get did not return a readable body")
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(body.read, self._chunk_size)
                except (BotoCoreError, ClientError, OSError):
                    raise ArtifactStoreError("S3 get_object body read failed") from None
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ArtifactVerificationError("S3 get returned a non-bytes chunk")
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                await asyncio.to_thread(close)

    def _put_blocking(self, request: ArtifactWriteRequest) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": request.key,
            "ContentLength": request.size_bytes,
            "ContentType": request.content_type,
            "IfNoneMatch": "*",
            "Metadata": self._request_metadata(request),
        }
        if self._canonical:
            parameters.update(
                ObjectLockMode=request.retention.mode.value,
                ObjectLockRetainUntilDate=request.retention.retain_until,
            )
        try:
            descriptor = self._open_source(request.source_path)
        except OSError as error:
            raise ArtifactVerificationError(
                f"artifact source could not be opened errno={error.errno}"
            ) from error
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as raw:
                parameters["Body"] = _BoundedFileBody(raw, chunk_size=self._chunk_size)
                try:
                    response = self._client.put_object(**parameters)
                except ClientError as error:
                    code = self._client_error_code(error)
                    if code in _OBJECT_EXISTS_CODES:
                        raise _ObjectAlreadyExists from None
                    raise ArtifactStoreError(f"S3 put_object failed code={code}") from None
                except BotoCoreError as error:
                    raise ArtifactStoreError(
                        f"S3 put_object failed type={type(error).__name__}"
                    ) from None
        except OSError as error:
            raise ArtifactVerificationError(
                f"artifact source could not be opened errno={error.errno}"
            ) from error
        if not isinstance(response, dict):
            raise ArtifactVerificationError("S3 put returned malformed metadata")
        return response

    @staticmethod
    def _open_source(path: Path) -> int:
        if path.is_symlink():
            raise ArtifactVerificationError("artifact source must not be a symlink")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(descriptor)
            raise ArtifactVerificationError("artifact source must be a regular file")
        return descriptor

    async def _call(self, operation: str, **parameters: Any) -> dict[str, Any]:
        try:
            response = await asyncio.to_thread(
                getattr(self._client, operation),
                **parameters,
            )
        except ClientError as error:
            code = self._client_error_code(error)
            if code in _MISSING_CODES:
                raise MissingArtifactError(
                    f"S3 {operation} object does not exist code={code}"
                ) from None
            raise ArtifactStoreError(f"S3 {operation} failed code={code}") from None
        except BotoCoreError as error:
            raise ArtifactStoreError(f"S3 {operation} failed type={type(error).__name__}") from None
        if not isinstance(response, dict):
            raise ArtifactVerificationError(f"S3 {operation} returned malformed metadata")
        return response

    def _stored_from_head(
        self,
        key: str,
        response: dict[str, Any],
        *,
        requested_version_id: str | None,
    ) -> StoredArtifact:
        metadata = response.get("Metadata")
        if not isinstance(metadata, dict):
            raise ArtifactVerificationError("S3 object is missing MAAIS metadata")
        try:
            sha256 = str(metadata["maais-sha256"])
            size_bytes = int(metadata["maais-size"])
            content_type = str(metadata["maais-content-type"])
            retention_mode = RetentionMode(str(metadata["maais-retention-mode"]))
            retain_until = _parse_utc(str(metadata["maais-retain-until"]))
            etag = str(response["ETag"])
            stored_at_raw = response["LastModified"]
            if not isinstance(stored_at_raw, datetime):
                raise TypeError
            stored_at = stored_at_raw.astimezone(timezone.utc)
            content_length = int(response["ContentLength"])
            provider_content_type = str(response["ContentType"])
        except (KeyError, TypeError, ValueError) as error:
            raise ArtifactVerificationError(
                "S3 object metadata is incomplete or invalid"
            ) from error
        if content_length != size_bytes or provider_content_type != content_type:
            raise ArtifactVerificationError("S3 object headers conflict with MAAIS metadata")

        raw_version_id = response.get("VersionId")
        version_id = str(raw_version_id) if raw_version_id is not None else None
        if requested_version_id is not None and version_id != requested_version_id:
            raise ArtifactVerificationError("S3 head returned an unexpected version ID")
        if self._canonical:
            if not version_id:
                raise StoreCapabilityError("canonical S3 head did not return a version ID")
            provider_mode = response.get("ObjectLockMode")
            provider_until = response.get("ObjectLockRetainUntilDate")
            if provider_mode != retention_mode.value or not isinstance(provider_until, datetime):
                raise ArtifactVerificationError("canonical S3 retention metadata is missing")
            if provider_until.astimezone(timezone.utc) != retain_until:
                raise ArtifactVerificationError("canonical S3 retention deadline differs")
        return StoredArtifact(
            store_name=self._store_name,
            key=key,
            etag=etag,
            version_id=version_id,
            sha256=sha256,
            size_bytes=size_bytes,
            content_type=content_type,
            retention=RetentionRequest(mode=retention_mode, retain_until=retain_until),
            stored_at=stored_at,
        )

    @staticmethod
    def _request_metadata(request: ArtifactWriteRequest) -> dict[str, str]:
        return {
            "maais-sha256": request.sha256,
            "maais-size": str(request.size_bytes),
            "maais-content-type": request.content_type,
            "maais-retention-mode": request.retention.mode.value,
            "maais-retain-until": _utc_iso(request.retention.retain_until),
        }

    @staticmethod
    def _verify_metadata_matches(
        request: ArtifactWriteRequest,
        stored: StoredArtifact,
        *,
        collision: bool,
    ) -> None:
        if (
            stored.key == request.key
            and stored.sha256 == request.sha256
            and stored.size_bytes == request.size_bytes
            and stored.content_type == request.content_type
            and stored.retention == request.retention
        ):
            return
        if collision:
            raise ArtifactCollisionError("S3 artifact key collision")
        raise ArtifactVerificationError("S3 stored metadata differs from upload request")

    async def _verify_read_back(
        self,
        request: ArtifactWriteRequest,
        stored: StoredArtifact,
    ) -> None:
        digest = hashlib.sha256()
        size_bytes = 0
        async for chunk in self.read_chunks(request.key, version_id=stored.version_id):
            digest.update(chunk)
            size_bytes += len(chunk)
        if size_bytes != request.size_bytes:
            raise ArtifactVerificationError("S3 read-back size differs from upload request")
        if digest.hexdigest() != request.sha256:
            raise ArtifactVerificationError("S3 read-back SHA-256 differs from upload request")

    @staticmethod
    def _client_error_code(error: ClientError) -> str:
        response_error = error.response.get("Error", {})
        if not isinstance(response_error, dict):
            return "Unknown"
        code = response_error.get("Code")
        rendered = str(code) if code is not None else "Unknown"
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", rendered) is None:
            return "Unknown"
        return rendered
