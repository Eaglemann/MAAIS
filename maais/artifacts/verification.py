from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

from maais.artifacts.models import (
    DEFAULT_HASH_CHUNK_SIZE,
    BundleFileExpectation,
    FileDigest,
    VerifiedBundleFile,
    validate_object_key,
)
from maais.artifacts.store import ArtifactVerificationError


def hash_file(path: Path, *, chunk_size: int = DEFAULT_HASH_CHUNK_SIZE) -> FileDigest:
    if chunk_size <= 0:
        raise ValueError("artifact hash chunk size must be positive")
    if path.is_symlink():
        raise ArtifactVerificationError("artifact source must not be a symlink")
    if not path.is_file():
        raise ArtifactVerificationError("artifact source must be a regular file")
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
            size_bytes += len(chunk)
    return FileDigest(sha256=digest.hexdigest(), size_bytes=size_bytes)


def _resolve_bundle_file(bundle_root: Path, relative_path: str) -> Path:
    if bundle_root.is_symlink():
        raise ArtifactVerificationError("bundle root must not be a symlink")
    try:
        root = bundle_root.resolve(strict=True)
    except OSError as error:
        raise ArtifactVerificationError("bundle root does not exist") from error
    if not root.is_dir():
        raise ArtifactVerificationError("bundle root must be a directory")
    try:
        validate_object_key(relative_path)
    except ValueError as error:
        raise ArtifactVerificationError("artifact file must remain inside bundle root") from error

    candidate = root
    for part in relative_path.split("/"):
        candidate /= part
        if candidate.is_symlink():
            raise ArtifactVerificationError("artifact bundle must not contain symlinks")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise ArtifactVerificationError("artifact file must remain inside bundle root") from error
    if not resolved.is_file():
        raise ArtifactVerificationError("artifact bundle entry must be a regular file")
    return resolved


def verify_bundle_file(
    *,
    bundle_root: Path,
    relative_path: str,
    expected_sha256: str,
    expected_size_bytes: int,
    content_type: str,
) -> VerifiedBundleFile:
    source = _resolve_bundle_file(bundle_root, relative_path)
    try:
        expectation = BundleFileExpectation(
            relative_path=relative_path,
            sha256=expected_sha256,
            size_bytes=expected_size_bytes,
            content_type=content_type,
        )
    except ValueError as error:
        raise ArtifactVerificationError("artifact bundle metadata is invalid") from error
    actual = hash_file(source)
    if actual.size_bytes != expectation.size_bytes:
        raise ArtifactVerificationError(
            f"artifact size mismatch: expected={expectation.size_bytes} actual={actual.size_bytes}"
        )
    if actual.sha256 != expectation.sha256:
        raise ArtifactVerificationError("artifact SHA-256 mismatch")
    return VerifiedBundleFile(
        path=source,
        relative_path=expectation.relative_path,
        sha256=actual.sha256,
        size_bytes=actual.size_bytes,
        content_type=expectation.content_type,
    )


def verify_bundle(
    bundle_root: Path,
    entries: Iterable[BundleFileExpectation],
) -> tuple[VerifiedBundleFile, ...]:
    verified: list[VerifiedBundleFile] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.relative_path in seen:
            raise ArtifactVerificationError(f"duplicate bundle path: {entry.relative_path}")
        seen.add(entry.relative_path)
        verified.append(
            verify_bundle_file(
                bundle_root=bundle_root,
                relative_path=entry.relative_path,
                expected_sha256=entry.sha256,
                expected_size_bytes=entry.size_bytes,
                content_type=entry.content_type,
            )
        )
    return tuple(verified)
