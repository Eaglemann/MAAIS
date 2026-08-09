from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from maais.artifacts.filesystem import FilesystemArtifactStore
from maais.artifacts.s3 import S3ArtifactStore
from maais.artifacts.store import ArtifactStore
from tests.contracts.artifacts import ArtifactStoreContract


class _MemoryS3Client:
    def __init__(self, *, canonical: bool) -> None:
        self._canonical = canonical
        self._objects: dict[str, dict[str, Any]] = {}

    def get_bucket_versioning(self, **parameters: Any) -> dict[str, Any]:
        return {"Status": "Enabled" if self._canonical else "Suspended"}

    def get_object_lock_configuration(self, **parameters: Any) -> dict[str, Any]:
        if not self._canonical:
            return {}
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}

    def put_object(self, **parameters: Any) -> dict[str, Any]:
        key = str(parameters["Key"])
        if key in self._objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed", "Message": "already exists"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        body = parameters["Body"]
        chunks: list[bytes] = []
        while chunk := body.read():
            chunks.append(chunk)
        content = b"".join(chunks)
        assert len(content) == parameters["ContentLength"]
        version_id = "version-001" if self._canonical else None
        stored = {
            "Body": content,
            "ContentLength": len(content),
            "ContentType": parameters["ContentType"],
            "ETag": '"multipart-etag-2"',
            "LastModified": datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
            "Metadata": dict(parameters["Metadata"]),
        }
        if version_id is not None:
            stored.update(
                VersionId=version_id,
                ObjectLockMode=parameters["ObjectLockMode"],
                ObjectLockRetainUntilDate=parameters["ObjectLockRetainUntilDate"],
            )
        self._objects[key] = stored
        response = {"ETag": stored["ETag"]}
        if version_id is not None:
            response["VersionId"] = version_id
        return response

    def head_object(self, **parameters: Any) -> dict[str, Any]:
        stored = self._get(parameters, operation="HeadObject")
        return {name: value for name, value in stored.items() if name != "Body"}

    def get_object(self, **parameters: Any) -> dict[str, Any]:
        stored = self._get(parameters, operation="GetObject")
        content = stored["Body"]
        assert isinstance(content, bytes)
        return {
            "Body": StreamingBody(BytesIO(content), len(content)),
            "ContentLength": len(content),
        }

    def _get(self, parameters: dict[str, Any], *, operation: str) -> dict[str, Any]:
        key = str(parameters["Key"])
        stored = self._objects.get(key)
        if stored is None:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation,
            )
        requested_version = parameters.get("VersionId")
        if requested_version is not None and requested_version != stored.get("VersionId"):
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchVersion", "Message": "missing"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation,
            )
        return stored


class TestFilesystemArtifactStore(ArtifactStoreContract):
    @pytest.fixture
    def artifact_store(self, tmp_path: Path) -> ArtifactStore:
        return FilesystemArtifactStore(root=tmp_path / "filesystem-store")


class TestReplicaS3ArtifactStore(ArtifactStoreContract):
    @pytest.fixture
    def artifact_store(self, tmp_path: Path) -> ArtifactStore:
        return S3ArtifactStore(
            client=_MemoryS3Client(canonical=False),
            bucket="replica",
            canonical=False,
        )


class TestCanonicalS3ArtifactStore(ArtifactStoreContract):
    @pytest.fixture
    def artifact_store(self, tmp_path: Path) -> ArtifactStore:
        return S3ArtifactStore(
            client=_MemoryS3Client(canonical=True),
            bucket="archive",
            canonical=True,
        )
