"""Derive and atomically persist the canonical cloud candidate descriptor."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from maais.domain.json import canonical_json_bytes, content_hash
from maais.experiments.prepare import capture_repository_identity
from maais.platform.identity import CandidateDescriptor


def build_candidate_descriptor(
    *,
    repository_root: Path,
    dashboard_dist: Path,
    git_sha: str,
    source_clean: bool,
) -> CandidateDescriptor:
    """Build an official candidate identity only from explicit values and file bytes."""

    if source_clean is not True:
        raise ValueError("official candidate requires a clean source assertion")
    if len(git_sha) != 40 or any(character not in "0123456789abcdef" for character in git_sha):
        raise ValueError("git_sha must be 40 lowercase hexadecimal characters")
    root = repository_root.resolve(strict=True)

    def supplied_build_identity(arguments: tuple[str, ...], called_root: Path) -> bytes:
        if called_root != root:
            raise ValueError("candidate identity git root changed unexpectedly")
        if arguments == ("rev-parse", "HEAD"):
            return f"{git_sha}\n".encode("ascii")
        if arguments == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            return b""
        raise ValueError(f"unexpected candidate identity Git operation: {arguments!r}")

    repository = capture_repository_identity(root, git_runner=supplied_build_identity)
    dashboard_root = dashboard_dist if dashboard_dist.is_absolute() else root / dashboard_dist
    _manifest, asset_manifest_hash = build_dashboard_asset_manifest(dashboard_root)
    dashboard_lock = _repository_file(root, root / "dashboard" / "package-lock.json")
    build_definition = _repository_file(root, root / "Dockerfile")
    return CandidateDescriptor.build(
        git_sha=repository.git_sha,
        source_clean=True,
        uv_lock_sha256=repository.lock_hash,
        dashboard_lock_sha256=_file_hash(dashboard_lock),
        schema_revision=repository.schema_revision,
        agent_implementation_hashes=repository.agent_implementation_hashes,
        dashboard_asset_manifest_sha256=asset_manifest_hash,
        build_definition_sha256=_file_hash(build_definition),
    )


def build_dashboard_asset_manifest(
    dashboard_dist: Path,
    *,
    asset_paths: Sequence[Path] | None = None,
) -> tuple[dict[str, Any], str]:
    """Return the sorted content-addressed dashboard inventory and its canonical hash."""

    try:
        root = dashboard_dist.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("dashboard asset inventory requires at least one regular file") from exc
    if not root.is_dir():
        raise ValueError("dashboard asset inventory root must be a directory")
    paths = tuple(asset_paths) if asset_paths is not None else tuple(root.rglob("*"))
    assets: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_path in paths:
        candidate_path = raw_path if raw_path.is_absolute() else root / raw_path
        try:
            path = candidate_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"dashboard asset is missing: {raw_path}") from exc
        if raw_path.is_symlink() or not path.is_file():
            if asset_paths is not None:
                raise ValueError(f"dashboard asset is not a regular file: {raw_path}")
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("dashboard asset path escapes the distribution root") from exc
        if relative in seen:
            raise ValueError(f"duplicate dashboard asset path: {relative}")
        seen.add(relative)
        content = path.read_bytes()
        assets.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if not assets:
        raise ValueError("dashboard asset inventory requires at least one regular file")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "assets": sorted(assets, key=lambda asset: str(asset["path"])),
    }
    return manifest, content_hash(manifest)


def write_candidate_descriptor(descriptor: CandidateDescriptor, path: Path) -> None:
    """Write a verified descriptor atomically with public, read-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.parent.resolve(strict=True) / path.name
    if target.is_symlink():
        raise ValueError("candidate descriptor output must not be a symbolic link")
    if target.exists() and not target.is_file():
        raise ValueError("candidate descriptor output must be a regular file")
    payload = canonical_json_bytes(descriptor.to_json_data()) + b"\n"
    temporary = _write_temporary(payload, target.parent)
    try:
        descriptor_from_payload = CandidateDescriptor.from_path(temporary)
        if descriptor_from_payload != descriptor:
            raise ValueError("candidate descriptor changed during serialization")
        os.replace(temporary, target)
        os.chmod(target, 0o644)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_temporary(payload: bytes, parent: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".candidate-", suffix=".json", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        stream = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    try:
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _repository_file(root: Path, path: Path) -> Path:
    candidate = path.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("candidate identity input escapes the repository") from exc
    if not candidate.is_file():
        raise ValueError(f"candidate identity input is not a regular file: {path}")
    return candidate


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
