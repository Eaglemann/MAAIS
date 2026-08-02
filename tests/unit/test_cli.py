import json
from pathlib import Path

import pytest

from maais.cli import build_parser, main
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
    verify_ledger = parser.parse_args(["verify-ledger"])
    report = parser.parse_args(
        [
            "daily-report",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--date",
            "2026-08-02",
            "--output",
            "artifacts/reports",
        ]
    )
    backup = parser.parse_args(["backup", "--output", "backups"])
    restore = parser.parse_args(
        [
            "restore",
            "--backup",
            "backups/candidate",
            "--target-database",
            "maais_week_restore",
            "--confirm-target",
            "maais_week_restore",
            "--output",
            "artifacts/restore-drills",
        ]
    )
    preflight = parser.parse_args(
        [
            "preflight",
            "--manifest",
            "artifacts/manifests/week.json",
            "--restore-verification",
            "artifacts/restore-drills/latest/restore-verification.json",
        ]
    )

    assert prepare.output == Path("candidate.json")
    assert not prepare.force
    assert run.manifest == Path("candidate.json")
    assert mission_control.port == 8000
    assert verify_ledger.command == "verify-ledger"
    assert report.report_date.isoformat() == "2026-08-02"
    assert report.output == Path("artifacts/reports")
    assert backup.output == Path("backups")
    assert restore.backup == Path("backups/candidate")
    assert restore.target_database == "maais_week_restore"
    assert preflight.repository == Path.cwd()
    assert preflight.minimum_free_gb == 5

    with pytest.raises(SystemExit):
        parser.parse_args(["mission-control", "--port", "0"])


def test_verify_ledger_prints_machine_readable_result_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_verification() -> dict[str, object]:
        return {"ok": True, "error_count": 0, "errors": []}

    monkeypatch.setattr("maais.cli.verify_configured_ledger", fake_verification)

    assert main(["verify-ledger"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "error_count": 0,
        "errors": [],
        "ok": True,
    }


def test_verify_ledger_returns_failure_when_consistency_errors_exist(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_verification() -> dict[str, object]:
        return {
            "ok": False,
            "error_count": 1,
            "errors": [
                {
                    "code": "stream_gap",
                    "aggregate_type": "experiment",
                    "aggregate_id": "00000000-0000-0000-0000-000000000001",
                    "details": "expected versions [1, 2], found [1]",
                }
            ],
        }

    monkeypatch.setattr("maais.cli.verify_configured_ledger", fake_verification)

    assert main(["verify-ledger"]) == 1
    assert json.loads(capsys.readouterr().out)["error_count"] == 1


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
