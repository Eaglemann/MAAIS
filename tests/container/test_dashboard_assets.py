from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from scripts.verify_dashboard_assets import verify_dashboard_assets


def test_inventory_can_require_one_exact_release(tmp_path: Path) -> None:
    release = "a" * 40
    content = b"safe"
    (tmp_path / "index.js").write_bytes(content)
    payload = {
        "schema_version": 1,
        "release": release,
        "assets": [
            {
                "path": "index.js",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    (tmp_path / "asset-manifest.json").write_text(
        json.dumps(
            {
                **payload,
                "manifest_hash": hashlib.sha256(canonical).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    verify_dashboard_assets(tmp_path, expected_release=release)
    with pytest.raises(AssertionError, match="release does not match"):
        verify_dashboard_assets(tmp_path, expected_release="b" * 40)


def test_inventory_rejects_source_maps(tmp_path: Path) -> None:
    (tmp_path / "app.js.map").write_text("{}", encoding="utf-8")

    with pytest.raises(AssertionError, match="contains source maps"):
        verify_dashboard_assets(tmp_path)


def test_inventory_rejects_symlinked_assets(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-dashboard-asset.js"
    outside.write_text("external", encoding="utf-8")
    (tmp_path / "app.js").symlink_to(outside)

    with pytest.raises(AssertionError, match="contains symlinks"):
        verify_dashboard_assets(tmp_path)


def test_inventory_rejects_escaped_manifest_paths(tmp_path: Path) -> None:
    (tmp_path / "index.js").write_text("safe", encoding="utf-8")
    (tmp_path / "asset-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": None,
                "assets": [{"path": "../outside.js", "sha256": "0" * 64, "size": 0}],
                "manifest_hash": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="unsafe dashboard asset path"):
        verify_dashboard_assets(tmp_path)


def test_final_dashboard_assets_are_map_free_and_hash_verified() -> None:
    configured = os.environ.get("MAAIS_DASHBOARD_ASSET_DIR")
    directory = Path(configured) if configured else Path("dashboard/dist")
    if configured is None and not directory.is_dir():
        pytest.skip("dashboard assets are verified by the frontend job after build")

    verify_dashboard_assets(directory)


def test_sentry_release_job_is_push_only_exact_release_and_secret_scoped() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    release_job = jobs["frontend-sentry-release"]

    assert release_job["if"] == (
        "github.event_name == 'push' && github.event.repository.fork == false"
    )
    assert release_job["env"]["SENTRY_RELEASE"] == "${{ github.sha }}"
    assert release_job["env"]["VITE_SENTRY_RELEASE"] == "${{ github.sha }}"
    assert "SENTRY_AUTH_TOKEN" not in release_job["env"]
    serialized = json.dumps(release_job, sort_keys=True)
    assert "sentry-cli releases info" in serialized
    assert "scripts/verify_dashboard_assets.py dashboard/dist-sourcemaps" in serialized
    assert "actions/upload-artifact@v7" in serialized

    for name, job in jobs.items():
        if name != "frontend-sentry-release":
            assert "SENTRY_AUTH_TOKEN" not in json.dumps(job, sort_keys=True)


def test_vite_upload_build_deletes_maps_and_normal_build_disables_them() -> None:
    config = Path("dashboard/vite.config.ts").read_text(encoding="utf-8")

    assert 'outDir !== "dist-sourcemaps"' in config
    assert "filesToDeleteAfterUpload: `./${outDir}/**/*.map`" in config
    assert 'sourcemap: sourceMapUpload ? "hidden" : false' in config
    assert "SENTRY_AUTH_TOKEN" in config
    assert "VITE_SENTRY_AUTH_TOKEN" not in config


def test_asset_manifest_writer_uses_locale_independent_path_order() -> None:
    writer = Path("dashboard/scripts/write-asset-manifest.mjs").read_text(encoding="utf-8")

    assert ".localeCompare(" not in writer
    assert "left.path < right.path" in writer
    assert "left.path > right.path" in writer
