from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from maais.platform.candidate import build_candidate_descriptor
from maais.platform.identity import CandidateDescriptor

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ENTRYPOINT = ["/opt/maais/.venv/bin/maais"]
EXPECTED_USER = "10001:10001"
FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "tests",
    }
)


@dataclass(frozen=True, slots=True)
class ImageEntry:
    path: str
    mode: int
    uid: int
    gid: int
    kind: str
    data: bytes = b""
    linkname: str = ""


@dataclass(frozen=True, slots=True)
class ImageSnapshot:
    config: dict[str, Any]
    files: dict[str, ImageEntry]


def test_runtime_image_contract(tmp_path: Path) -> None:
    configured = os.environ.get("MAAIS_TEST_IMAGE")
    if not configured:
        pytest.skip("set MAAIS_TEST_IMAGE to an OCI layout/archive or docker-save archive")
    snapshot = load_image_snapshot(Path(configured))
    runtime = snapshot.config.get("config")
    assert isinstance(runtime, dict)
    assert runtime.get("User") == EXPECTED_USER
    assert runtime.get("Entrypoint") == EXPECTED_ENTRYPOINT
    assert runtime.get("Cmd") in (None, [])
    labels = runtime.get("Labels")
    assert isinstance(labels, dict)
    assert labels.get("io.maais.candidate.schema") == "1"
    assert labels.get("io.maais.safety.paper-only") == "true"
    revision = labels.get("org.opencontainers.image.revision")
    assert isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)

    descriptor_entry = _required_regular(snapshot, "app/candidate.json")
    descriptor = CandidateDescriptor.from_json_data(json.loads(descriptor_entry.data))
    assert descriptor.git_sha == revision
    assert descriptor_entry.uid == 0 and descriptor_entry.gid == 0
    assert descriptor_entry.mode & 0o222 == 0

    dashboard_files = {
        path.removeprefix("app/dashboard/"): entry
        for path, entry in snapshot.files.items()
        if path.startswith("app/dashboard/") and entry.kind == "file"
    }
    assert "index.html" in dashboard_files
    assert "asset-manifest.json" in dashboard_files
    expected = build_candidate_descriptor(
        repository_root=ROOT,
        dashboard_dist=_materialize_dashboard(dashboard_files, tmp_path / "dashboard"),
        git_sha=revision,
        source_clean=True,
    )
    assert descriptor == expected

    application_entries = tuple(
        entry
        for path, entry in snapshot.files.items()
        if path == "app"
        or path.startswith("app/")
        or path == "opt/maais"
        or path.startswith("opt/maais/")
    )
    _assert_application_inventory_permissions(application_entries)
    _assert_forbidden_inventory_absent(snapshot)


def test_oci_layout_reader_merges_layers_and_honors_whiteouts(tmp_path: Path) -> None:
    first = _tar_layer(
        {
            "app/old.txt": b"old",
            "app/keep.txt": b"keep",
        }
    )
    second = _tar_layer(
        {
            "app/.wh.old.txt": b"",
            "app/new.txt": b"new",
        }
    )
    config = {"config": {"User": EXPECTED_USER, "Entrypoint": EXPECTED_ENTRYPOINT}}
    layout = _synthetic_oci_layout(tmp_path, config=config, layers=(first, second))

    snapshot = load_image_snapshot(layout)

    assert "app/old.txt" not in snapshot.files
    assert snapshot.files["app/keep.txt"].data == b"keep"
    assert snapshot.files["app/new.txt"].data == b"new"
    assert snapshot.config == config


def test_oci_layout_reader_rejects_layer_path_traversal(tmp_path: Path) -> None:
    layer = _tar_layer({"../escape": b"forbidden"})
    layout = _synthetic_oci_layout(tmp_path, config={"config": {}}, layers=(layer,))

    with pytest.raises(AssertionError, match="unsafe image layer path"):
        load_image_snapshot(layout)


def test_application_permission_contract_ignores_link_mode_bits() -> None:
    entries = (
        ImageEntry(path="app", mode=0o555, uid=0, gid=0, kind="dir"),
        ImageEntry(path="app/data", mode=0o444, uid=0, gid=0, kind="file"),
        ImageEntry(
            path="opt/maais/.venv/bin/python",
            mode=0o777,
            uid=0,
            gid=0,
            kind="symlink",
            linkname="/usr/local/bin/python3.12",
        ),
    )

    _assert_application_inventory_permissions(entries)


def test_application_permission_contract_rejects_writable_regular_entry() -> None:
    entries = (
        ImageEntry(path="app", mode=0o555, uid=0, gid=0, kind="dir"),
        ImageEntry(path="app/data", mode=0o644, uid=0, gid=0, kind="file"),
    )

    with pytest.raises(AssertionError, match=r"app/data.*0o644"):
        _assert_application_inventory_permissions(entries)


def test_oci_layout_reader_rejects_blob_bytes_that_do_not_match_descriptor(
    tmp_path: Path,
) -> None:
    layout = _synthetic_oci_layout(
        tmp_path,
        config={"config": {}},
        layers=(_tar_layer({"app/value": b"expected"}),),
    )
    index = json.loads((layout / "index.json").read_bytes())
    manifest_digest = index["manifests"][0]["digest"]
    manifest = json.loads((layout / _blob_path(manifest_digest)).read_bytes())
    layer_path = layout / _blob_path(manifest["layers"][0]["digest"])
    layer_path.write_bytes(b"x" * layer_path.stat().st_size)

    with pytest.raises(AssertionError, match="digest mismatch"):
        load_image_snapshot(layout)


def load_image_snapshot(path: Path) -> ImageSnapshot:
    if path.is_dir():
        return _load_oci_layout(path)
    if not path.is_file():
        raise AssertionError(f"image archive does not exist: {path}")
    with tarfile.open(path, mode="r:*") as archive:
        members = {
            normalized: member
            for member in archive.getmembers()
            if (normalized := _safe_image_path(member.name))
        }
        if "oci-layout" in members:
            blobs = {
                name: _read_member(archive, member)
                for name, member in members.items()
                if name.startswith("blobs/sha256/") and member.isfile()
            }
            index = _json_bytes(_read_member(archive, members["index.json"]), "OCI index")
            return _snapshot_from_oci(index, lambda digest: blobs[_blob_path(digest)])
        manifest = _json_bytes(
            _read_member(archive, members["manifest.json"]),
            "docker-save manifest",
        )
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise AssertionError("docker-save archive must contain exactly one image")
        image = manifest[0]
        if not isinstance(image, dict):
            raise AssertionError("docker-save image entry must be an object")
        config_name = image.get("Config")
        layers = image.get("Layers")
        if not isinstance(config_name, str) or not isinstance(layers, list):
            raise AssertionError("docker-save image entry is incomplete")
        config = _json_bytes(_read_member(archive, members[config_name]), "image config")
        layer_bytes = tuple(
            _read_member(archive, members[name]) for name in layers if isinstance(name, str)
        )
        if len(layer_bytes) != len(layers):
            raise AssertionError("docker-save layer inventory is invalid")
    return ImageSnapshot(config=_object(config, "image config"), files=_merge_layers(layer_bytes))


def _load_oci_layout(path: Path) -> ImageSnapshot:
    layout = _object(
        _json_bytes((path / "oci-layout").read_bytes(), "OCI layout"),
        "OCI layout",
    )
    if layout.get("imageLayoutVersion") != "1.0.0":
        raise AssertionError("unsupported OCI layout version")
    index = _json_bytes((path / "index.json").read_bytes(), "OCI index")

    def read_blob(digest: str) -> bytes:
        return (path / _blob_path(digest)).read_bytes()

    return _snapshot_from_oci(index, read_blob)


def _snapshot_from_oci(index: object, read_blob) -> ImageSnapshot:
    manifest = _resolve_oci_manifest(_object(index, "OCI index"), read_blob)
    config_descriptor = _object(manifest.get("config"), "OCI config descriptor")
    config = _object(
        _json_bytes(_read_verified_blob(config_descriptor, read_blob), "image config"),
        "image config",
    )
    raw_layers = manifest.get("layers")
    if not isinstance(raw_layers, list) or not raw_layers:
        raise AssertionError("OCI manifest requires layers")
    layers: list[bytes] = []
    for raw_descriptor in raw_layers:
        descriptor = _object(raw_descriptor, "OCI layer descriptor")
        media_type = descriptor.get("mediaType")
        raw = _read_verified_blob(descriptor, read_blob)
        if media_type in {
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
        }:
            raw = gzip.decompress(raw)
        elif media_type not in {
            "application/vnd.oci.image.layer.v1.tar",
            "application/vnd.docker.image.rootfs.diff.tar",
        }:
            raise AssertionError(f"unsupported OCI layer media type: {media_type!r}")
        layers.append(raw)
    return ImageSnapshot(config=config, files=_merge_layers(tuple(layers)))


def _resolve_oci_manifest(index: dict[str, Any], read_blob) -> dict[str, Any]:
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise AssertionError("OCI index requires a manifest")
    candidates = [
        _object(value, "OCI manifest descriptor") for value in manifests if isinstance(value, dict)
    ]
    preferred = next(
        (
            value
            for value in candidates
            if isinstance(value.get("platform"), dict)
            and value["platform"].get("os") == "linux"
            and value["platform"].get("architecture") == "amd64"
        ),
        candidates[0] if len(candidates) == 1 else None,
    )
    if preferred is None:
        raise AssertionError("OCI index has no unique linux/amd64 manifest")
    document = _object(
        _json_bytes(_read_verified_blob(preferred, read_blob), "OCI manifest"),
        "OCI manifest",
    )
    if "manifests" in document:
        return _resolve_oci_manifest(document, read_blob)
    if "config" not in document or "layers" not in document:
        raise AssertionError("OCI image manifest is incomplete")
    return document


def _merge_layers(layers: tuple[bytes, ...]) -> dict[str, ImageEntry]:
    files: dict[str, ImageEntry] = {}
    for raw_layer in layers:
        with tarfile.open(fileobj=io.BytesIO(raw_layer), mode="r:*") as archive:
            for member in archive:
                path = _safe_image_path(member.name)
                if not path:
                    continue
                name = PurePosixPath(path).name
                parent = PurePosixPath(path).parent.as_posix()
                if name == ".wh..wh..opq":
                    prefix = "" if parent == "." else f"{parent}/"
                    files = {
                        key: value for key, value in files.items() if not key.startswith(prefix)
                    }
                    continue
                if name.startswith(".wh."):
                    target = str(PurePosixPath(parent) / name.removeprefix(".wh."))
                    files = {
                        key: value
                        for key, value in files.items()
                        if key != target and not key.startswith(f"{target}/")
                    }
                    continue
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise AssertionError(f"image layer file cannot be read: {path}")
                    kind, data = "file", extracted.read()
                elif member.isdir():
                    kind, data = "dir", b""
                elif member.issym():
                    kind, data = "symlink", b""
                elif member.islnk():
                    kind, data = "hardlink", b""
                else:
                    raise AssertionError(f"unsupported special image file: {path}")
                files[path] = ImageEntry(
                    path=path,
                    mode=member.mode & 0o7777,
                    uid=member.uid,
                    gid=member.gid,
                    kind=kind,
                    data=data,
                    linkname=member.linkname,
                )
    return files


def _assert_forbidden_inventory_absent(snapshot: ImageSnapshot) -> None:
    for path, entry in snapshot.files.items():
        parts = set(PurePosixPath(path).parts)
        assert not parts.intersection(FORBIDDEN_PATH_PARTS), path
        assert not PurePosixPath(path).name.startswith(".env"), path
        assert not path.endswith((".map", ".pyc", ".pyo")), path
        assert "dist-sourcemaps" not in parts, path
        if entry.kind == "file" and len(entry.data) <= 5 * 1024 * 1024:
            assert b"SENTRY_AUTH_TOKEN" not in entry.data, path
            assert b"BINANCE_DEMO_API_SECRET" not in entry.data, path
    for executable in ("pip", "pip3", "uv", "uvx"):
        assert f"opt/maais/.venv/bin/{executable}" not in snapshot.files
        assert f"usr/local/bin/{executable}" not in snapshot.files
    assert not any("ensurepip" in PurePosixPath(path).parts for path in snapshot.files)
    assert not any(
        path.startswith("usr/local/lib/python3.12/site-packages/pip") for path in snapshot.files
    )


def _assert_application_inventory_permissions(entries: tuple[ImageEntry, ...]) -> None:
    assert entries
    non_root = tuple(entry.path for entry in entries if entry.uid != 0 or entry.gid != 0)
    assert not non_root, f"application entries must be root-owned: {non_root!r}"

    # Linux does not consult permission bits on symbolic links, and tar hard-link
    # headers do not define the target inode's effective mode. Regular files and
    # directories are the entries whose write bits make the packaged tree mutable.
    writable = tuple(
        f"{entry.path} ({oct(entry.mode)})"
        for entry in entries
        if entry.kind in {"file", "dir"} and entry.mode & 0o222
    )
    assert not writable, f"application entries must be non-writable: {writable!r}"


def _materialize_dashboard(files: dict[str, ImageEntry], target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for relative, entry in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry.data)
    return target


def _required_regular(snapshot: ImageSnapshot, path: str) -> ImageEntry:
    entry = snapshot.files.get(path)
    if entry is None or entry.kind != "file":
        raise AssertionError(f"runtime image requires regular file /{path}")
    return entry


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise AssertionError(f"archive member cannot be read: {member.name}")
    return extracted.read()


def _safe_image_path(raw: str) -> str:
    normalized = raw
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise AssertionError(f"unsafe image layer path: {raw!r}")
    return path.as_posix() if path.as_posix() != "." else ""


def _digest(descriptor: dict[str, Any]) -> str:
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise AssertionError("OCI descriptor digest is invalid")
    return digest


def _read_verified_blob(descriptor: dict[str, Any], read_blob) -> bytes:
    digest = _digest(descriptor)
    size = descriptor.get("size")
    if type(size) is not int or size < 0:
        raise AssertionError("OCI descriptor size is invalid")
    raw = read_blob(digest)
    if len(raw) != size:
        raise AssertionError("OCI blob size mismatch")
    observed = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if observed != digest:
        raise AssertionError("OCI blob digest mismatch")
    return raw


def _blob_path(digest: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise AssertionError("OCI blob digest is invalid")
    return f"blobs/sha256/{digest.removeprefix('sha256:')}"


def _json_bytes(raw: bytes, label: str) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"{label} is invalid JSON") from error


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be an object")
    return value


def _tar_layer(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.mode = 0o444
            member.uid = 0
            member.gid = 0
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def _synthetic_oci_layout(
    root: Path,
    *,
    config: dict[str, Any],
    layers: tuple[bytes, ...],
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
    config_descriptor = _write_blob(
        root,
        json.dumps(config, separators=(",", ":")).encode(),
        "application/vnd.oci.image.config.v1+json",
    )
    layer_descriptors = [
        _write_blob(root, raw, "application/vnd.oci.image.layer.v1.tar") for raw in layers
    ]
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config_descriptor,
        "layers": layer_descriptors,
    }
    manifest_descriptor = _write_blob(
        root,
        json.dumps(manifest, separators=(",", ":")).encode(),
        "application/vnd.oci.image.manifest.v1+json",
    )
    manifest_descriptor["platform"] = {"os": "linux", "architecture": "amd64"}
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [manifest_descriptor],
    }
    (root / "index.json").write_text(
        json.dumps(index, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return root


def _write_blob(root: Path, raw: bytes, media_type: str) -> dict[str, object]:
    digest = hashlib.sha256(raw).hexdigest()
    path = root / "blobs" / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "mediaType": media_type,
        "digest": f"sha256:{digest}",
        "size": len(raw),
    }
