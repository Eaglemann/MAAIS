import json
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

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
    health = parser.parse_args(
        [
            "health",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--maximum-lag-seconds",
            "180",
        ]
    )
    acknowledge = parser.parse_args(
        [
            "acknowledge-incident",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--incident",
            "22222222-2222-4222-8222-222222222222",
            "--actor",
            "denis",
        ]
    )
    resolve = parser.parse_args(
        [
            "resolve-incident",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--incident",
            "22222222-2222-4222-8222-222222222222",
            "--actor",
            "denis",
            "--resolution",
            "transient venue timestamp skew reviewed against the next cycle",
            "--confirm-reviewed",
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
    assert health.maximum_lag_seconds == 180
    assert not health.allow_stopped
    assert acknowledge.actor == "denis"
    assert resolve.confirm_reviewed
    assert resolve.resolution.startswith("transient venue timestamp")

    with pytest.raises(SystemExit):
        parser.parse_args(["mission-control", "--port", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "resolve-incident",
                "--experiment",
                "11111111-1111-4111-8111-111111111111",
                "--incident",
                "22222222-2222-4222-8222-222222222222",
                "--actor",
                "denis",
                "--resolution",
                "reviewed",
            ]
        )


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


def test_daily_report_refuses_to_freeze_an_incomplete_berlin_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"complete_day": False}

    monkeypatch.setattr("maais.cli.build_configured_daily_report", fake_report)

    with pytest.raises(ValueError, match="Berlin day is incomplete"):
        main(
            [
                "daily-report",
                "--experiment",
                "11111111-1111-4111-8111-111111111111",
                "--date",
                "2026-08-02",
                "--output",
                str(tmp_path),
            ]
        )


def test_daily_report_requires_explicit_partial_override_for_stop_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "complete_day": False,
            "report_id": "a" * 64,
            "reconciliation": {"ledger_ok": True},
        }

    target = tmp_path / "partial-report"
    monkeypatch.setattr("maais.cli.build_configured_daily_report", fake_report)
    monkeypatch.setattr(
        "maais.cli.write_daily_report_bundle",
        lambda *_args, **_kwargs: SimpleNamespace(
            directory=target,
            json_path=target / "report.json",
            markdown_path=target / "report.md",
            decisions_csv_path=target / "decisions.csv",
            decisions_parquet_path=target / "decisions.parquet",
            execution_csv_path=target / "execution.csv",
            execution_parquet_path=target / "execution.parquet",
            manifest_path=target / "bundle-manifest.json",
        ),
    )

    assert (
        main(
            [
                "daily-report",
                "--experiment",
                "11111111-1111-4111-8111-111111111111",
                "--date",
                "2026-08-02",
                "--output",
                str(tmp_path),
                "--allow-partial",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["directory"] == str(target)


def test_final_report_cli_aggregates_the_exact_seven_day_evidence_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_build(reports: Path, **kwargs: object) -> dict[str, object]:
        captured["reports"] = reports
        captured.update(kwargs)
        return {"report_id": "f" * 64}

    target = tmp_path / "final" / "bundle"
    monkeypatch.setattr(
        "maais.cli.build_final_report_from_bundles",
        fake_build,
        raising=False,
    )
    monkeypatch.setattr(
        "maais.cli.write_final_report_bundle",
        lambda *_args, **_kwargs: SimpleNamespace(
            directory=target,
            json_path=target / "report.json",
            markdown_path=target / "report.md",
            daily_reports_csv_path=target / "daily-reports.csv",
            manifest_path=target / "bundle-manifest.json",
        ),
        raising=False,
    )

    assert (
        main(
            [
                "final-report",
                "--experiment",
                "11111111-1111-4111-8111-111111111111",
                "--start-date",
                "2026-08-03",
                "--reports",
                str(tmp_path / "daily"),
                "--output",
                str(tmp_path / "final"),
            ]
        )
        == 0
    )
    assert captured["reports"] == tmp_path / "daily"
    assert captured["experiment_id"] == UUID("11111111-1111-4111-8111-111111111111")
    assert str(captured["start_date"]) == "2026-08-03"
    assert captured["days"] == 7
    assert captured["generated_at"].utcoffset() == timezone.utc.utcoffset(None)  # type: ignore[union-attr]
    assert json.loads(capsys.readouterr().out)["directory"] == str(target)


def test_resolve_incident_prints_audited_machine_readable_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    async def fake_action(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "incident_id": "22222222-2222-4222-8222-222222222222",
            "status": "resolved",
            "version": 3,
            "event_type": "incident.resolved",
            "content_hash": "a" * 64,
        }

    monkeypatch.setattr("maais.cli.apply_configured_incident_action", fake_action)

    assert (
        main(
            [
                "resolve-incident",
                "--experiment",
                "11111111-1111-4111-8111-111111111111",
                "--incident",
                "22222222-2222-4222-8222-222222222222",
                "--actor",
                "denis",
                "--resolution",
                "transient source skew reviewed against the next cycle",
                "--confirm-reviewed",
            ]
        )
        == 0
    )

    assert str(captured["action"]) == "resolve"
    assert captured["operator_confirmed"] is True
    assert json.loads(capsys.readouterr().out)["event_type"] == "incident.resolved"


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
        binance_demo_api_key="configured",  # pragma: allowlist secret
    )

    with pytest.raises(ValueError, match="refuses configured exchange credentials"):
        await run_live_paper_manifest(manifest, settings=settings)
