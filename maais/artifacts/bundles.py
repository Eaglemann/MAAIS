from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from maais.artifacts.models import (
    DEFAULT_HASH_CHUNK_SIZE,
    BundleFileExpectation,
    VerifiedBundleFile,
    validate_object_key,
    validate_sha256,
)
from maais.artifacts.store import ArtifactVerificationError
from maais.artifacts.verification import hash_file, verify_bundle
from maais.domain.json import content_hash as canonical_content_hash

MAX_BUNDLE_MANIFEST_BYTES: Final = 2 * 1024 * 1024
MAX_SEMANTIC_JSON_BYTES: Final = 64 * 1024 * 1024
BUNDLE_MANIFEST_NAME: Final = "bundle-manifest.json"

_CONTENT_TYPES: Final[Mapping[str, str]] = {
    ".csv": "text/csv",
    ".dump": "application/octet-stream",
    ".gz": "application/gzip",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".ndjson": "application/x-ndjson",
    ".parquet": "application/vnd.apache.parquet",
    ".txt": "text/plain",
    ".zip": "application/zip",
    ".zst": "application/zstd",
}


@dataclass(frozen=True, slots=True)
class VerifiedArtifactBundle:
    directory: Path
    report_id: str
    content_hash: str
    files: tuple[VerifiedBundleFile, ...]
    size_bytes: int
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if not self.directory.is_absolute():
            raise ValueError("verified bundle directory must be absolute")
        validate_sha256(self.report_id)
        validate_sha256(self.content_hash)
        if not self.files:
            raise ValueError("verified bundle inventory must not be empty")
        if tuple(sorted(item.relative_path for item in self.files)) != tuple(
            item.relative_path for item in self.files
        ):
            raise ValueError("verified bundle inventory must be sorted")
        if len({item.relative_path for item in self.files}) != len(self.files):
            raise ValueError("verified bundle paths must be unique")
        if self.size_bytes != sum(item.size_bytes for item in self.files):
            raise ValueError("verified bundle size differs from its inventory")


def validate_bundle(
    directory: Path,
    *,
    expected_report_id: str,
) -> VerifiedArtifactBundle:
    """Validate a canonical self-hashed evidence bundle without trusting filenames."""
    validate_sha256(expected_report_id)
    root = _validated_root(directory)
    manifest_path = root / BUNDLE_MANIFEST_NAME
    manifest = _load_canonical_json(
        manifest_path,
        maximum_bytes=MAX_BUNDLE_MANIFEST_BYTES,
        label="bundle manifest",
    )
    if not isinstance(manifest, dict):
        raise ArtifactVerificationError("bundle manifest must be a JSON object")
    if manifest.get("report_id") != expected_report_id:
        raise ArtifactVerificationError("bundle report identity differs from publication")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, dict) or not raw_artifacts:
        raise ArtifactVerificationError("bundle manifest artifacts must be a nonempty object")

    expectations: list[BundleFileExpectation] = []
    for relative_path, raw_identity in raw_artifacts.items():
        if not isinstance(relative_path, str):
            raise ArtifactVerificationError("bundle artifact path must be a string")
        try:
            validate_object_key(relative_path)
        except ValueError as error:
            raise ArtifactVerificationError("bundle artifact path is invalid") from error
        if relative_path == BUNDLE_MANIFEST_NAME:
            raise ArtifactVerificationError("bundle manifest cannot hash itself")
        if not isinstance(raw_identity, dict) or set(raw_identity) != {"bytes", "sha256"}:
            raise ArtifactVerificationError("bundle artifact identity is not exact")
        size_bytes = raw_identity.get("bytes")
        sha256 = raw_identity.get("sha256")
        if type(size_bytes) is not int or size_bytes < 0 or not isinstance(sha256, str):
            raise ArtifactVerificationError("bundle artifact identity is invalid")
        try:
            expectations.append(
                BundleFileExpectation(
                    relative_path=relative_path,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    content_type=_content_type(relative_path),
                )
            )
        except ValueError as error:
            raise ArtifactVerificationError("bundle artifact identity is invalid") from error

    expected_paths = {item.relative_path for item in expectations} | {BUNDLE_MANIFEST_NAME}
    _validate_directory_inventory(root, expected_paths)
    verified = list(verify_bundle(root, sorted(expectations, key=lambda item: item.relative_path)))
    manifest_digest = hash_file(manifest_path)
    verified.append(
        VerifiedBundleFile(
            path=manifest_path,
            relative_path=BUNDLE_MANIFEST_NAME,
            sha256=manifest_digest.sha256,
            size_bytes=manifest_digest.size_bytes,
            content_type="application/json",
        )
    )
    verified.sort(key=lambda item: item.relative_path)
    _verify_semantic_report_identity(verified, expected_report_id)
    bundle_hash = canonical_content_hash(
        [
            {
                "content_type": item.content_type,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in verified
        ]
    )
    return VerifiedArtifactBundle(
        directory=root,
        report_id=expected_report_id,
        content_hash=bundle_hash,
        files=tuple(verified),
        size_bytes=sum(item.size_bytes for item in verified),
    )


@contextmanager
def stage_verified_bundle(
    bundle: VerifiedArtifactBundle,
) -> Iterator[VerifiedArtifactBundle]:
    """Freeze verified bytes in an owner-only temporary directory for upload."""
    with tempfile.TemporaryDirectory(prefix="maais-artifact-publish-") as temporary:
        root = Path(temporary).resolve(strict=True)
        os.chmod(root, 0o700)
        staged_files: list[VerifiedBundleFile] = []
        for source in bundle.files:
            destination = root / source.relative_path
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _chmod_parents(destination.parent, root)
            _copy_verified(source, destination)
            staged_files.append(
                VerifiedBundleFile(
                    path=destination,
                    relative_path=source.relative_path,
                    sha256=source.sha256,
                    size_bytes=source.size_bytes,
                    content_type=source.content_type,
                )
            )
        yield VerifiedArtifactBundle(
            directory=root,
            report_id=bundle.report_id,
            content_hash=bundle.content_hash,
            files=tuple(staged_files),
            size_bytes=bundle.size_bytes,
            media_type=bundle.media_type,
        )


def _validated_root(directory: Path) -> Path:
    if directory.is_symlink():
        raise ArtifactVerificationError("artifact bundle root must not be a symlink")
    try:
        root = directory.resolve(strict=True)
    except OSError as error:
        raise ArtifactVerificationError("artifact bundle directory does not exist") from error
    if not root.is_dir():
        raise ArtifactVerificationError("artifact bundle root must be a directory")
    return root


def _validate_directory_inventory(root: Path, expected_paths: set[str]) -> None:
    actual_files: set[str] = set()
    expected_directories = {
        parent.as_posix()
        for path in expected_paths
        for parent in Path(path).parents
        if parent != Path(".")
    }
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ArtifactVerificationError("artifact bundle must not contain symlinks")
        relative = entry.relative_to(root).as_posix()
        if entry.is_dir():
            if relative not in expected_directories:
                raise ArtifactVerificationError("artifact bundle contains an unexpected directory")
            continue
        if not entry.is_file():
            raise ArtifactVerificationError("artifact bundle entries must be regular files")
        actual_files.add(relative)
    if actual_files != expected_paths:
        raise ArtifactVerificationError("artifact bundle contains missing or unexpected files")


def _verify_semantic_report_identity(
    files: list[VerifiedBundleFile],
    expected_report_id: str,
) -> None:
    matched = False
    for item in files:
        if item.relative_path == BUNDLE_MANIFEST_NAME or item.content_type != "application/json":
            continue
        value = _load_canonical_json(
            item.path,
            maximum_bytes=MAX_SEMANTIC_JSON_BYTES,
            label="bundle JSON artifact",
        )
        if isinstance(value, dict) and value.get("report_id") == expected_report_id:
            matched = True
    if not matched:
        raise ArtifactVerificationError("bundle has no semantic report identity match")


def _load_canonical_json(path: Path, *, maximum_bytes: int, label: str) -> object:
    raw = _read_bounded_regular(path, maximum_bytes=maximum_bytes, label=label)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        canonical = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise ArtifactVerificationError(f"{label} is not valid JSON") from error
    if raw != canonical:
        raise ArtifactVerificationError(f"{label} is not canonical JSON")
    return value


def _read_bounded_regular(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    if maximum_bytes <= 0:
        raise ValueError("maximum JSON size must be positive")
    if path.is_symlink():
        raise ArtifactVerificationError(f"{label} must not be a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArtifactVerificationError(f"{label} could not be opened") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactVerificationError(f"{label} must be a regular file")
        if metadata.st_size > maximum_bytes:
            raise ArtifactVerificationError(f"{label} exceeds the bounded JSON size")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(DEFAULT_HASH_CHUNK_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > maximum_bytes or os.read(descriptor, 1):
            raise ArtifactVerificationError(f"{label} exceeds the bounded JSON size")
        return raw
    finally:
        os.close(descriptor)


def _content_type(relative_path: str) -> str:
    content_type = _CONTENT_TYPES.get(Path(relative_path).suffix.casefold())
    if content_type is None:
        raise ArtifactVerificationError("bundle artifact extension has no approved content type")
    return content_type


def _copy_verified(source: VerifiedBundleFile, destination: Path) -> None:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        destination_flags |= os.O_NOFOLLOW
    try:
        source_descriptor = os.open(source.path, source_flags)
    except OSError as error:
        raise ArtifactVerificationError("artifact staging file could not be opened") from error
    try:
        destination_descriptor = os.open(destination, destination_flags, 0o600)
    except OSError as error:
        os.close(source_descriptor)
        raise ArtifactVerificationError("artifact staging file could not be opened") from error
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ArtifactVerificationError("artifact staging source must be a regular file")
        while chunk := os.read(source_descriptor, DEFAULT_HASH_CHUNK_SIZE):
            digest.update(chunk)
            size_bytes += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                view = view[written:]
        os.fsync(destination_descriptor)
    except OSError as error:
        raise ArtifactVerificationError("artifact staging copy failed") from error
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)
    os.chmod(destination, 0o600)
    if size_bytes != source.size_bytes or digest.hexdigest() != source.sha256:
        raise ArtifactVerificationError("artifact bytes changed while being staged")


def _chmod_parents(directory: Path, root: Path) -> None:
    current = directory
    while current != root:
        os.chmod(current, 0o700)
        current = current.parent
