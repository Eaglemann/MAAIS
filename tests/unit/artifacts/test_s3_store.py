from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from maais.artifacts.models import ArtifactWriteRequest, RetentionRequest
from maais.artifacts.s3 import MAX_S3_UPLOAD_CHUNK_SIZE, S3ArtifactStore, _BoundedFileBody
from maais.artifacts.store import (
    ArtifactStoreError,
    ArtifactVerificationError,
    StoreCapabilityError,
)
from maais.config.artifacts import RetentionMode

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
RETAIN_UNTIL = datetime(2027, 8, 8, tzinfo=timezone.utc)


def _request(
    tmp_path: Path,
    *,
    mode: RetentionMode = RetentionMode.COMPLIANCE,
) -> ArtifactWriteRequest:
    source = tmp_path / "report.json"
    source.write_bytes(b'{"status":"ready"}\n')
    content = source.read_bytes()
    return ArtifactWriteRequest(
        key="maais/qualification/candidate/run/report/report-001/report.json",
        source_path=source,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
        retention=RetentionRequest(mode=mode, retain_until=RETAIN_UNTIL),
    )


def _stubbed_store(*, canonical: bool) -> tuple[S3ArtifactStore, Stubber]:
    client = boto3.client(
        "s3",
        region_name="eu-central-1",
        endpoint_url="https://s3.example.invalid",
        aws_access_key_id="unit-access-key",  # pragma: allowlist secret
        aws_secret_access_key="unit-secret-key",  # pragma: allowlist secret
    )
    return (
        S3ArtifactStore(
            client=client,
            bucket="archive" if canonical else "replica",
            canonical=canonical,
        ),
        Stubber(client),
    )


def _metadata(request: ArtifactWriteRequest) -> dict[str, str]:
    return {
        "maais-sha256": request.sha256,
        "maais-size": str(request.size_bytes),
        "maais-content-type": request.content_type,
        "maais-retention-mode": request.retention.mode.value,
        "maais-retain-until": "2027-08-08T00:00:00Z",
    }


def _put_parameters(
    request: ArtifactWriteRequest,
    *,
    canonical: bool,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "Bucket": "archive" if canonical else "replica",
        "Key": request.key,
        "Body": ANY,
        "ContentLength": request.size_bytes,
        "ContentType": request.content_type,
        "IfNoneMatch": "*",
        "Metadata": _metadata(request),
    }
    if canonical:
        parameters.update(
            ObjectLockMode=request.retention.mode.value,
            ObjectLockRetainUntilDate=request.retention.retain_until,
        )
    return parameters


def _head_response(
    request: ArtifactWriteRequest,
    *,
    canonical: bool,
    etag: str = '"multipart-etag-3"',
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ContentLength": request.size_bytes,
        "ContentType": request.content_type,
        "ETag": etag,
        "LastModified": NOW,
        "Metadata": _metadata(request),
    }
    if canonical:
        response.update(
            VersionId="version-001",
            ObjectLockMode=request.retention.mode.value,
            ObjectLockRetainUntilDate=request.retention.retain_until,
        )
    return response


def _queue_canonical_capabilities(stubber: Stubber) -> None:
    stubber.add_response(
        "get_bucket_versioning",
        {"Status": "Enabled"},
        {"Bucket": "archive"},
    )
    stubber.add_response(
        "get_object_lock_configuration",
        {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}},
        {"Bucket": "archive"},
    )


def test_canonical_capabilities_require_enabled_versioning() -> None:
    store, stubber = _stubbed_store(canonical=True)
    stubber.add_response(
        "get_bucket_versioning",
        {"Status": "Suspended"},
        {"Bucket": "archive"},
    )

    with stubber, pytest.raises(StoreCapabilityError, match="versioning is not enabled"):
        import asyncio

        asyncio.run(store.capabilities())


def test_canonical_capabilities_require_object_lock() -> None:
    store, stubber = _stubbed_store(canonical=True)
    stubber.add_response(
        "get_bucket_versioning",
        {"Status": "Enabled"},
        {"Bucket": "archive"},
    )
    stubber.add_response("get_object_lock_configuration", {}, {"Bucket": "archive"})

    with stubber, pytest.raises(StoreCapabilityError, match="Object Lock is not enabled"):
        import asyncio

        asyncio.run(store.capabilities())


@pytest.mark.asyncio
async def test_railway_replica_reports_unsupported_versioning_without_probing() -> None:
    store, stubber = _stubbed_store(canonical=False)

    with stubber:
        capabilities = await store.capabilities()

    assert capabilities.versioning_enabled is False
    assert capabilities.object_lock_enabled is False
    assert capabilities.exact_version_reads is False
    assert capabilities.immutable_create is True


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_mode", tuple(RetentionMode))
async def test_canonical_put_applies_retention_and_verifies_exact_version(
    tmp_path: Path,
    retention_mode: RetentionMode,
) -> None:
    request = _request(tmp_path, mode=retention_mode)
    store, stubber = _stubbed_store(canonical=True)
    _queue_canonical_capabilities(stubber)
    stubber.add_response(
        "put_object",
        {"ETag": '"multipart-etag-3"', "VersionId": "version-001"},
        _put_parameters(request, canonical=True),
    )
    stubber.add_response(
        "head_object",
        _head_response(request, canonical=True),
        {"Bucket": "archive", "Key": request.key, "VersionId": "version-001"},
    )
    stubber.add_response(
        "get_object",
        {
            "Body": StreamingBody(BytesIO(request.source_path.read_bytes()), request.size_bytes),
            "ContentLength": request.size_bytes,
        },
        {"Bucket": "archive", "Key": request.key, "VersionId": "version-001"},
    )

    with stubber:
        result = await store.put_verified(request)

    assert result.artifact.version_id == "version-001"
    assert result.artifact.etag == '"multipart-etag-3"'
    assert result.artifact.retention.mode is retention_mode


@pytest.mark.asyncio
async def test_canonical_put_rejects_absent_version_id(tmp_path: Path) -> None:
    request = _request(tmp_path)
    store, stubber = _stubbed_store(canonical=True)
    _queue_canonical_capabilities(stubber)
    stubber.add_response(
        "put_object",
        {"ETag": '"etag"'},
        _put_parameters(request, canonical=True),
    )

    with stubber, pytest.raises(StoreCapabilityError, match="version ID"):
        await store.put_verified(request)


@pytest.mark.asyncio
async def test_canonical_put_rejects_missing_provider_retention_metadata(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    store, stubber = _stubbed_store(canonical=True)
    _queue_canonical_capabilities(stubber)
    stubber.add_response(
        "put_object",
        {"ETag": '"etag"', "VersionId": "version-001"},
        _put_parameters(request, canonical=True),
    )
    response = _head_response(request, canonical=True, etag='"etag"')
    response.pop("ObjectLockRetainUntilDate")
    stubber.add_response(
        "head_object",
        response,
        {"Bucket": "archive", "Key": request.key, "VersionId": "version-001"},
    )

    with stubber, pytest.raises(ArtifactVerificationError, match="retention metadata"):
        await store.put_verified(request)


@pytest.mark.asyncio
async def test_put_always_recomputes_read_back_sha256(tmp_path: Path) -> None:
    request = _request(tmp_path)
    store, stubber = _stubbed_store(canonical=True)
    _queue_canonical_capabilities(stubber)
    stubber.add_response(
        "put_object",
        {"ETag": '"etag"', "VersionId": "version-001"},
        _put_parameters(request, canonical=True),
    )
    stubber.add_response(
        "head_object",
        _head_response(request, canonical=True, etag='"etag"'),
        {"Bucket": "archive", "Key": request.key, "VersionId": "version-001"},
    )
    wrong = b"x" * request.size_bytes
    stubber.add_response(
        "get_object",
        {"Body": StreamingBody(BytesIO(wrong), len(wrong)), "ContentLength": len(wrong)},
        {"Bucket": "archive", "Key": request.key, "VersionId": "version-001"},
    )

    with stubber, pytest.raises(ArtifactVerificationError, match="read-back SHA-256"):
        await store.put_verified(request)


@pytest.mark.asyncio
async def test_exact_version_read_passes_version_id_to_provider(tmp_path: Path) -> None:
    request = _request(tmp_path)
    store, stubber = _stubbed_store(canonical=True)
    stubber.add_response(
        "get_object",
        {
            "Body": StreamingBody(BytesIO(request.source_path.read_bytes()), request.size_bytes),
            "ContentLength": request.size_bytes,
        },
        {"Bucket": "archive", "Key": request.key, "VersionId": "version-001"},
    )

    with stubber:
        content = b"".join(
            [
                chunk
                async for chunk in store.read_chunks(
                    request.key,
                    version_id="version-001",
                )
            ]
        )

    assert content == request.source_path.read_bytes()


@pytest.mark.asyncio
async def test_provider_errors_expose_only_stable_code(tmp_path: Path) -> None:
    request = _request(tmp_path)
    store, stubber = _stubbed_store(canonical=False)
    stubber.add_client_error(
        "put_object",
        service_error_code="AccessDenied",
        service_message="credential-canary must never escape",  # pragma: allowlist secret
        http_status_code=403,
        expected_params=_put_parameters(request, canonical=False),
    )

    with stubber, pytest.raises(ArtifactStoreError) as error:
        await store.put_verified(request)

    assert "AccessDenied" in str(error.value)
    assert "credential-canary" not in str(error.value)
    assert error.value.__cause__ is None


def test_upload_body_never_reads_more_than_eight_mebibytes(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"a" * (MAX_S3_UPLOAD_CHUNK_SIZE * 2 + 17))

    with source.open("rb") as raw:
        body = _BoundedFileBody(raw, chunk_size=MAX_S3_UPLOAD_CHUNK_SIZE)
        chunks = (body.read(), body.read(), body.read(), body.read())

    assert tuple(map(len, chunks)) == (
        MAX_S3_UPLOAD_CHUNK_SIZE,
        MAX_S3_UPLOAD_CHUNK_SIZE,
        17,
        0,
    )
    assert MAX_S3_UPLOAD_CHUNK_SIZE == 8 * 1024 * 1024
