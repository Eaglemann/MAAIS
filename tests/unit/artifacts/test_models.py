from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from maais.artifacts.models import (
    DEFAULT_HASH_CHUNK_SIZE,
    ArtifactWriteRequest,
    BundleFileExpectation,
    RetentionRequest,
    artifact_key,
    validate_object_key,
)
from maais.artifacts.verification import (
    ArtifactVerificationError,
    hash_file,
    verify_bundle,
    verify_bundle_file,
)
from maais.config.artifacts import RetentionMode

EXPERIMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
RETAIN_UNTIL = datetime(2027, 8, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "key",
    (
        "/absolute",
        "../escape",
        "run/../escape",
        "run/./report.json",
        "run/latest/report.json",
        "run/LATEST/report.json",
        "double//slash",
        "back\\slash",
        "trailing/slash/",
        "percent/%2e%2e/report.json",
        "control/\n/report.json",
    ),
)
def test_artifact_key_rejects_unsafe_or_mutable_paths(key: str) -> None:
    with pytest.raises(ValueError):
        validate_object_key(key)


def test_canonical_artifact_key_binds_every_identity_segment() -> None:
    key = artifact_key(
        environment="qualification",
        candidate_hash="a" * 64,
        experiment_id=EXPERIMENT_ID,
        artifact_type="soak_verdict",
        report_id="report-001",
        relative_path="evidence/verdict.json",
    )

    assert key == (
        "maais/qualification/"
        f"{'a' * 64}/{EXPERIMENT_ID}/soak_verdict/report-001/evidence/verdict.json"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("environment", "production/escape"),
        ("candidate_hash", "A" * 64),
        ("candidate_hash", "a" * 63),
        ("artifact_type", "daily report"),
        ("report_id", "latest"),
        ("relative_path", "../report.json"),
    ),
)
def test_canonical_artifact_key_rejects_invalid_identity_segments(
    field: str,
    value: str,
) -> None:
    values: dict[str, object] = {
        "environment": "qualification",
        "candidate_hash": "a" * 64,
        "experiment_id": EXPERIMENT_ID,
        "artifact_type": "daily_report",
        "report_id": "report-001",
        "relative_path": "report.json",
    }
    values[field] = value

    with pytest.raises(ValueError):
        artifact_key(**values)  # type: ignore[arg-type]


def test_retention_requires_an_aware_utc_timestamp() -> None:
    assert (
        RetentionRequest(
            mode=RetentionMode.COMPLIANCE,
            retain_until=RETAIN_UNTIL,
        ).retain_until
        == RETAIN_UNTIL
    )

    with pytest.raises(ValueError, match="UTC"):
        RetentionRequest(
            mode=RetentionMode.COMPLIANCE,
            retain_until=datetime(2027, 8, 8),
        )
    with pytest.raises(ValueError, match="UTC"):
        RetentionRequest(
            mode=RetentionMode.COMPLIANCE,
            retain_until=datetime(2027, 8, 8, tzinfo=timezone(timedelta(hours=2))),
        )


def test_hash_file_uses_fixed_one_mebibyte_chunks(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    content = b"a" * (DEFAULT_HASH_CHUNK_SIZE + 17)
    source.write_bytes(content)

    digest = hash_file(source)

    assert digest.sha256 == hashlib.sha256(content).hexdigest()
    assert digest.size_bytes == len(content)
    assert DEFAULT_HASH_CHUNK_SIZE == 1024 * 1024


@pytest.mark.parametrize(
    ("expected_sha256", "expected_size", "message"),
    (
        ("0" * 64, 7, "SHA-256"),
        (hashlib.sha256(b"payload").hexdigest(), 8, "size"),
    ),
)
def test_bundle_file_rejects_mismatched_hash_or_size(
    tmp_path: Path,
    expected_sha256: str,
    expected_size: int,
    message: str,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "report.json").write_bytes(b"payload")

    with pytest.raises(ArtifactVerificationError, match=message):
        verify_bundle_file(
            bundle_root=bundle,
            relative_path="report.json",
            expected_sha256=expected_sha256,
            expected_size_bytes=expected_size,
            content_type="application/json",
        )


def test_bundle_rejects_duplicate_relative_paths(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    content = b"payload"
    (bundle / "report.json").write_bytes(content)
    entry = BundleFileExpectation(
        relative_path="report.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        content_type="application/json",
    )

    with pytest.raises(ArtifactVerificationError, match="duplicate bundle path"):
        verify_bundle(bundle, (entry, entry))


def test_bundle_file_rejects_symlinks_and_outside_paths(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"payload")
    (bundle / "linked.json").symlink_to(outside)
    digest = hashlib.sha256(b"payload").hexdigest()

    with pytest.raises(ArtifactVerificationError, match="symlink"):
        verify_bundle_file(
            bundle_root=bundle,
            relative_path="linked.json",
            expected_sha256=digest,
            expected_size_bytes=7,
            content_type="application/json",
        )
    with pytest.raises(ArtifactVerificationError, match="inside bundle root"):
        verify_bundle_file(
            bundle_root=bundle,
            relative_path="../outside.json",
            expected_sha256=digest,
            expected_size_bytes=7,
            content_type="application/json",
        )


def test_write_request_rejects_unapproved_mime_and_invalid_metadata(tmp_path: Path) -> None:
    source = tmp_path / "report.json"
    source.write_bytes(b"payload")
    retention = RetentionRequest(
        mode=RetentionMode.COMPLIANCE,
        retain_until=RETAIN_UNTIL,
    )

    with pytest.raises(ValueError, match="content type"):
        ArtifactWriteRequest(
            key="maais/qualification/report.json",
            source_path=source,
            sha256=hashlib.sha256(b"payload").hexdigest(),
            size_bytes=7,
            content_type="text/html",
            retention=retention,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        ArtifactWriteRequest(
            key="maais/qualification/report.json",
            source_path=source,
            sha256="invalid",
            size_bytes=7,
            content_type="application/json",
            retention=retention,
        )
