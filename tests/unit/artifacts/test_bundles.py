from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from maais.artifacts.bundles import stage_verified_bundle, validate_bundle
from maais.artifacts.store import ArtifactVerificationError

REPORT_ID = "a" * 64


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _write_bundle(root: Path, *, report_id: str = REPORT_ID) -> Path:
    root.mkdir()
    report = _canonical_json({"report_id": report_id, "status": "ready"})
    (root / "report.json").write_bytes(report)
    manifest = {
        "artifacts": {
            "report.json": {
                "bytes": len(report),
                "sha256": hashlib.sha256(report).hexdigest(),
            }
        },
        "report_id": REPORT_ID,
        "report_schema_version": 1,
    }
    (root / "bundle-manifest.json").write_bytes(_canonical_json(manifest))
    return root


def test_bundle_validation_derives_complete_sorted_inventory_and_content_hash(
    tmp_path: Path,
) -> None:
    bundle = validate_bundle(_write_bundle(tmp_path / "bundle"), expected_report_id=REPORT_ID)

    assert bundle.report_id == REPORT_ID
    assert tuple(item.relative_path for item in bundle.files) == (
        "bundle-manifest.json",
        "report.json",
    )
    assert bundle.size_bytes == sum(item.size_bytes for item in bundle.files)
    assert len(bundle.content_hash) == 64
    assert bundle.media_type == "application/octet-stream"


@pytest.mark.parametrize("mutation", ("unexpected", "hash", "size", "report_id"))
def test_bundle_validation_rejects_manifest_or_identity_mismatch(
    tmp_path: Path,
    mutation: str,
) -> None:
    directory = _write_bundle(tmp_path / "bundle")
    if mutation == "unexpected":
        (directory / "unlisted.txt").write_text("not cataloged", encoding="utf-8")
    elif mutation in {"hash", "size"}:
        manifest_path = directory / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["report.json"]["sha256" if mutation == "hash" else "bytes"] = (
            "0" * 64 if mutation == "hash" else 1
        )
        manifest_path.write_bytes(_canonical_json(manifest))
    else:
        (directory / "report.json").write_bytes(
            _canonical_json({"report_id": "b" * 64, "status": "ready"})
        )
        report = (directory / "report.json").read_bytes()
        manifest_path = directory / "bundle-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["report.json"] = {
            "bytes": len(report),
            "sha256": hashlib.sha256(report).hexdigest(),
        }
        manifest_path.write_bytes(_canonical_json(manifest))

    with pytest.raises(ArtifactVerificationError):
        validate_bundle(directory, expected_report_id=REPORT_ID)


def test_bundle_validation_rejects_noncanonical_json_and_symlinks(tmp_path: Path) -> None:
    noncanonical = _write_bundle(tmp_path / "noncanonical")
    manifest = json.loads((noncanonical / "bundle-manifest.json").read_text(encoding="utf-8"))
    (noncanonical / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactVerificationError, match="canonical JSON"):
        validate_bundle(noncanonical, expected_report_id=REPORT_ID)

    symlinked = _write_bundle(tmp_path / "symlinked")
    target = symlinked / "report.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ArtifactVerificationError, match="symlink"):
        validate_bundle(symlinked, expected_report_id=REPORT_ID)


def test_private_staging_is_byte_verified_and_always_removed(tmp_path: Path) -> None:
    source = validate_bundle(_write_bundle(tmp_path / "bundle"), expected_report_id=REPORT_ID)
    staged_directory: Path | None = None

    with pytest.raises(RuntimeError, match="simulated upload failure"):
        with stage_verified_bundle(source) as staged:
            staged_directory = staged.directory
            assert stat.S_IMODE(staged.directory.stat().st_mode) == 0o700
            assert all(stat.S_IMODE(item.path.stat().st_mode) == 0o600 for item in staged.files)
            assert staged.content_hash == source.content_hash
            assert all(
                item.path.read_bytes() == (source.directory / item.relative_path).read_bytes()
                for item in staged.files
            )
            raise RuntimeError("simulated upload failure")

    assert staged_directory is not None
    assert not os.path.exists(staged_directory)


def test_private_staging_rejects_source_changed_after_validation(tmp_path: Path) -> None:
    source = validate_bundle(_write_bundle(tmp_path / "bundle"), expected_report_id=REPORT_ID)
    (source.directory / "report.json").write_bytes(
        _canonical_json({"report_id": REPORT_ID, "status": "changed"})
    )

    with pytest.raises(ArtifactVerificationError, match="changed while being staged"):
        with stage_verified_bundle(source):
            pytest.fail("staging must not yield after source mutation")
