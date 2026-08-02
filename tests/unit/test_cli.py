import json
from pathlib import Path

import pytest

from maais.cli import build_parser
from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.live import load_manifest_file, run_live_paper_manifest
from tests.unit.experiments.test_runtime_policy import _live_manifest


def test_operator_cli_requires_explicit_manifest_and_output_paths() -> None:
    parser = build_parser()

    prepare = parser.parse_args(
        [
            "prepare-paper-live",
            "--output",
            "candidate.json",
            "--name",
            "candidate",
        ]
    )
    run = parser.parse_args(["paper-live", "--manifest", "candidate.json"])
    mission_control = parser.parse_args(["mission-control"])

    assert prepare.output == Path("candidate.json")
    assert not prepare.force
    assert run.manifest == Path("candidate.json")
    assert mission_control.port == 8000

    with pytest.raises(SystemExit):
        parser.parse_args(["mission-control", "--port", "0"])


def test_manifest_file_loader_preserves_exact_identity(tmp_path: Path) -> None:
    manifest = _live_manifest(schema_revision="0015")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    restored = load_manifest_file(path)

    assert restored == manifest
    assert restored.manifest_hash == manifest.manifest_hash


async def test_paper_live_refuses_nonpaper_environment_before_database_access() -> None:
    manifest = _live_manifest(schema_revision="0015")
    settings = Settings(run_mode=RunMode.REPLAY)

    with pytest.raises(ValueError, match="RUN_MODE=paper_live"):
        await run_live_paper_manifest(manifest, settings=settings)


async def test_paper_live_refuses_even_demo_credentials() -> None:
    manifest = _live_manifest(schema_revision="0015")
    settings = Settings(
        run_mode=RunMode.PAPER_LIVE,
        binance_demo_api_key="configured",
    )

    with pytest.raises(ValueError, match="refuses configured exchange credentials"):
        await run_live_paper_manifest(manifest, settings=settings)
