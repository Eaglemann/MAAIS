import json
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

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
    database_identity = parser.parse_args(["database-identity"])
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
    daily_supervisor = parser.parse_args(["daily-supervisor"])
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
    cloud_publish = parser.parse_args(
        [
            "cloud-publish",
            "--run",
            "22222222-2222-4222-8222-222222222222",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--date",
            "2026-08-08",
            "--type",
            "daily_report",
            "--report-id",
            "a" * 64,
            "--bundle",
            "artifacts/report",
        ]
    )
    cloud_backup = parser.parse_args(
        [
            "cloud-backup",
            "--run",
            "22222222-2222-4222-8222-222222222222",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--date",
            "2026-08-08",
            "--output",
            "artifacts/cloud-backups",
        ]
    )
    cloud_restore = parser.parse_args(
        [
            "cloud-restore-verify",
            "--artifact-record",
            "88888888-8888-4888-8888-888888888888",
            "--output",
            "artifacts/cloud-restores",
        ]
    )
    preflight = parser.parse_args(
        [
            "preflight",
            "--manifest",
            "artifacts/manifests/week.json",
            "--restore-verification",
            "artifacts/restore-drills/latest/restore-verification.json",
            "--qualification",
            "artifacts/qualification/latest",
            "--soak-readiness",
            "artifacts/readiness/latest",
        ]
    )
    qualify = parser.parse_args(
        [
            "qualify-candidate",
            "--output",
            "artifacts/qualification",
        ]
    )
    process_drills = parser.parse_args(
        [
            "process-drill-verdict",
            "--manifest",
            "artifacts/manifests/drill.json",
            "--dashboard-baseline",
            "artifacts/drills/dashboard-baseline.json",
            "--dashboard-recovery",
            "artifacts/drills/dashboard-recovery.json",
            "--dashboard-after",
            "artifacts/drills/dashboard-after.json",
            "--worker-baseline",
            "artifacts/drills/worker-baseline.json",
            "--worker-recovery",
            "artifacts/drills/worker-recovery.json",
            "--worker-after",
            "artifacts/drills/worker-after.json",
            "--daily-close",
            "artifacts/drills/daily-close.json",
            "--output",
            "artifacts/process-drills",
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
    soak_verdict = parser.parse_args(
        [
            "soak-verdict",
            "--experiment",
            "11111111-1111-4111-8111-111111111111",
            "--output",
            "artifacts/readiness",
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
    assert database_identity.command == "database-identity"
    assert verify_ledger.command == "verify-ledger"
    assert report.report_date.isoformat() == "2026-08-02"
    assert report.output == Path("artifacts/reports")
    assert daily_supervisor.state == Path("artifacts/run-state/current.json")
    assert daily_supervisor.close_script == Path("scripts/daily-paper-ops.sh")
    assert daily_supervisor.poll_seconds == 30
    assert backup.output == Path("backups")
    assert restore.backup == Path("backups/candidate")
    assert restore.target_database == "maais_week_restore"
    assert cloud_publish.artifact_type == "daily_report"
    assert cloud_publish.bundle == Path("artifacts/report")
    assert cloud_backup.output == Path("artifacts/cloud-backups")
    assert cloud_restore.artifact_record == UUID("88888888-8888-4888-8888-888888888888")
    assert not hasattr(cloud_restore, "target_database_url")
    assert not hasattr(cloud_restore, "object_key")
    assert preflight.repository == Path.cwd()
    assert preflight.minimum_free_gb == 20
    assert preflight.qualification == Path("artifacts/qualification/latest")
    assert preflight.soak_readiness == Path("artifacts/readiness/latest")
    assert qualify.repository == Path.cwd()
    assert qualify.output == Path("artifacts/qualification")
    assert process_drills.repository == Path.cwd()
    assert process_drills.worker_after == Path("artifacts/drills/worker-after.json")
    assert health.maximum_lag_seconds == 180
    assert not health.allow_stopped
    assert soak_verdict.state == Path("artifacts/run-state/current.json")
    assert soak_verdict.maximum_lag_seconds == 180
    assert acknowledge.actor == "denis"
    assert resolve.confirm_reviewed
    assert resolve.resolution.startswith("transient venue timestamp")

    with pytest.raises(SystemExit):
        parser.parse_args(["mission-control", "--port", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "cloud-restore-verify",
                "--artifact-record",
                "88888888-8888-4888-8888-888888888888",
                "--output",
                "artifacts/cloud-restores",
                "--object-key",
                "untrusted/latest.dump",
            ]
        )
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


def test_qualification_uses_separate_test_database_environment_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = "postgresql+psycopg://localhost:5432/maais_test"
    bundle = tmp_path / "qualification-bundle"
    captured: dict[str, object] = {}

    def fake_qualification(**kwargs: object):
        captured.update(kwargs)
        return (
            SimpleNamespace(
                directory=bundle,
                report_path=bundle / "qualification.json",
                manifest_path=bundle / "bundle-manifest.json",
            ),
            {"passed": True, "report_id": "a" * 64},
        )

    monkeypatch.setenv("MAAIS_TEST_DATABASE_URL", database_url)
    monkeypatch.setattr("maais.cli.run_candidate_qualification", fake_qualification)

    assert main(["qualify-candidate", "--output", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert captured["test_database_url"] == database_url
    assert database_url not in output
    assert json.loads(output) == {
        "bundle_manifest": str(bundle / "bundle-manifest.json"),
        "directory": str(bundle),
        "passed": True,
        "qualification": str(bundle / "qualification.json"),
        "report_id": "a" * 64,
    }


def test_preflight_forwards_the_soak_readiness_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    async def fake_preflight(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"passed": False}

    soak_bundle = tmp_path / "soak-readiness"
    monkeypatch.setattr("maais.cli.run_candidate_preflight", fake_preflight)

    assert (
        main(
            [
                "preflight",
                "--manifest",
                "manifest.json",
                "--restore-verification",
                "restore.json",
                "--qualification",
                "qualification",
                "--soak-readiness",
                str(soak_bundle),
            ]
        )
        == 1
    )
    assert captured["soak_readiness_directory"] == soak_bundle
    assert json.loads(capsys.readouterr().out) == {"passed": False}


def test_process_drill_verdict_prints_only_the_frozen_bundle_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "process-drill-bundle"

    def fake_freeze(**_kwargs: object):
        return (
            SimpleNamespace(
                directory=bundle,
                report_path=bundle / "process-drills.json",
                manifest_path=bundle / "bundle-manifest.json",
            ),
            {"passed": True, "report_id": "c" * 64},
        )

    monkeypatch.setattr("maais.cli.freeze_process_drill_evidence", fake_freeze)
    arguments = [
        "process-drill-verdict",
        "--manifest",
        "manifest.json",
        "--dashboard-baseline",
        "dashboard-baseline.json",
        "--dashboard-recovery",
        "dashboard-recovery.json",
        "--dashboard-after",
        "dashboard-after.json",
        "--worker-baseline",
        "worker-baseline.json",
        "--worker-recovery",
        "worker-recovery.json",
        "--worker-after",
        "worker-after.json",
        "--daily-close",
        "daily-close.json",
        "--output",
        str(tmp_path),
    ]

    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {
        "bundle_manifest": str(bundle / "bundle-manifest.json"),
        "directory": str(bundle),
        "passed": True,
        "report": str(bundle / "process-drills.json"),
        "report_id": "c" * 64,
    }


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


def test_database_identity_prints_machine_readable_cluster_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_identity() -> dict[str, object]:
        return {
            "database": "maais",
            "system_identifier": "7669409277984608290",
            "server_address": "172.18.0.2",
            "server_port": 5432,
        }

    monkeypatch.setattr("maais.cli.collect_configured_database_identity", fake_identity)

    assert main(["database-identity"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "database": "maais",
        "server_address": "172.18.0.2",
        "server_port": 5432,
        "system_identifier": "7669409277984608290",
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


def test_daily_report_resumes_existing_complete_bundle_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "existing-report"

    async def unexpected_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("an existing complete bundle must not be rebuilt")

    monkeypatch.setattr("maais.cli.build_configured_daily_report", unexpected_report)
    monkeypatch.setattr(
        "maais.cli.resolve_existing_daily_report_bundle",
        lambda *_args, **_kwargs: {
            "report_id": "a" * 64,
            "directory": str(target),
            "json": str(target / "report.json"),
            "markdown": str(target / "report.md"),
            "decisions_csv": str(target / "decisions.csv"),
            "decisions_parquet": str(target / "decisions.parquet"),
            "execution_csv": str(target / "execution.csv"),
            "execution_parquet": str(target / "execution.parquet"),
            "bundle_manifest": str(target / "bundle-manifest.json"),
            "resumed": True,
        },
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
                "--resume-existing",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["resumed"] is True


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


def test_paper_live_cli_logs_a_terminal_failure_without_a_plain_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _live_manifest(schema_revision="0015")

    async def fail_worker(*_: object, **__: object) -> None:
        raise RuntimeError("public source retries exhausted")

    monkeypatch.setattr(
        "maais.cli.get_settings",
        lambda: Settings(environment="production", run_mode=RunMode.PAPER_LIVE),
    )
    monkeypatch.setattr("maais.cli.load_manifest_file", lambda _: manifest)
    monkeypatch.setattr("maais.cli.run_live_paper_manifest", fail_worker)

    assert main(["paper-live", "--manifest", "candidate.json"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert output.err == ""
    assert payload["event"] == "paper_live_failed"
    assert payload["error_code"] == "worker_unhandled_exception"
    assert payload["experiment_ref"] == str(manifest.experiment_id)
    assert payload["outcome"] == "halt_persistence_unknown"
    assert payload["exception"]["type"] == "RuntimeError"
    assert payload["exception"]["message"] == "public source retries exhausted"


async def test_paper_live_refuses_nonpaper_environment_before_database_access() -> None:
    manifest = _live_manifest(schema_revision="0015")
    settings = Settings(run_mode=RunMode.REPLAY)

    with pytest.raises(ValueError, match="RUN_MODE=paper_live"):
        await run_live_paper_manifest(manifest, settings=settings)


async def test_paper_live_refuses_even_demo_credentials() -> None:
    manifest = _live_manifest(schema_revision="0015")
    settings = Settings(
        run_mode=RunMode.PAPER_LIVE,
        binance_demo_api_key=SecretStr("configured"),  # pragma: allowlist secret
    )

    with pytest.raises(ValueError, match="refuses configured exchange credentials"):
        await run_live_paper_manifest(manifest, settings=settings)
