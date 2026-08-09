#!/usr/bin/env python3
"""Verify the map-free, content-hashed Mission Control asset inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def verify_dashboard_assets(directory: Path) -> dict[str, object]:
    if not directory.is_dir():
        raise AssertionError(f"dashboard asset directory does not exist: {directory}")
    if directory.is_symlink():
        raise AssertionError(f"dashboard asset directory cannot be a symlink: {directory}")
    directory = directory.resolve(strict=True)
    inventory = tuple(directory.rglob("*"))
    symlinks = tuple(sorted(path.relative_to(directory) for path in inventory if path.is_symlink()))
    if symlinks:
        raise AssertionError(f"deployable dashboard contains symlinks: {symlinks}")
    special_files = tuple(
        sorted(
            path.relative_to(directory)
            for path in inventory
            if not path.is_dir() and not path.is_file()
        )
    )
    if special_files:
        raise AssertionError(f"deployable dashboard contains special files: {special_files}")
    maps = tuple(sorted(path.relative_to(directory) for path in inventory if path.suffix == ".map"))
    if maps:
        raise AssertionError(f"deployable dashboard contains source maps: {maps}")

    manifest_path = directory / "asset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise AssertionError("dashboard asset manifest must be an object")
    if manifest.get("schema_version") != 1:
        raise AssertionError("unsupported dashboard asset manifest schema")
    release = manifest["release"]
    if not (
        release is None
        or (
            isinstance(release, str)
            and len(release) == 40
            and set(release) <= set("0123456789abcdef")
        )
    ):
        raise AssertionError("dashboard asset release must be a lowercase 40-character Git SHA")
    assets = manifest["assets"]
    if not isinstance(assets, list) or not assets:
        raise AssertionError("dashboard asset inventory cannot be empty")
    asset_paths: list[str] = []
    for item in assets:
        if not isinstance(item, dict):
            raise AssertionError("dashboard asset entry must be an object")
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise AssertionError("dashboard asset path must be a string")
        relative_path = PurePosixPath(raw_path)
        if (
            not raw_path
            or "\\" in raw_path
            or relative_path.is_absolute()
            or "." in relative_path.parts
            or ".." in relative_path.parts
        ):
            raise AssertionError(f"unsafe dashboard asset path: {raw_path!r}")
        asset_paths.append(raw_path)
    if len(asset_paths) != len(set(asset_paths)):
        raise AssertionError("dashboard asset inventory contains duplicate paths")
    expected_paths = sorted(
        path.relative_to(directory).as_posix()
        for path in inventory
        if path.is_file() and path != manifest_path
    )
    if asset_paths != expected_paths:
        raise AssertionError("dashboard asset inventory does not match deployable files")
    for item in assets:
        path = directory / item["path"]
        content = path.read_bytes()
        if item.get("size") != len(content):
            raise AssertionError(f"dashboard asset size mismatch: {item['path']}")
        if item.get("sha256") != hashlib.sha256(content).hexdigest():
            raise AssertionError(f"dashboard asset hash mismatch: {item['path']}")
        if b"SENTRY_AUTH_TOKEN" in content:
            raise AssertionError(
                f"dashboard asset contains a forbidden secret marker: {item['path']}"
            )
        if path.suffix in {".css", ".js"}:
            if b"sourceMappingURL=" in content:
                raise AssertionError(f"dashboard asset references a source map: {item['path']}")

    canonical = json.dumps(
        {
            "schema_version": manifest["schema_version"],
            "release": release,
            "assets": assets,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if manifest.get("manifest_hash") != hashlib.sha256(canonical).hexdigest():
        raise AssertionError("dashboard asset manifest hash mismatch")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    manifest = verify_dashboard_assets(arguments.directory)
    print(
        json.dumps(
            {
                "asset_count": len(manifest["assets"]),
                "manifest_hash": manifest["manifest_hash"],
                "release": manifest["release"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
