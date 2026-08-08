"""Local operator CLI for the paper worker and Mission Control."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from maais.config.settings import get_settings
from maais.core.logging import configure_logging
from maais.live import (
    load_manifest_file,
    prepare_live_manifest_file,
    run_live_paper_manifest,
)
from maais.operations.backups import backup_configured_database
from maais.operations.daily_supervisor import supervise_daily_closes
from maais.operations.database_identity import collect_configured_database_identity
from maais.operations.final_reporting import (
    build_final_report_from_bundles,
    resolve_existing_daily_report_bundle,
    write_final_report_bundle,
)
from maais.operations.health import collect_configured_experiment_health
from maais.operations.incident_management import (
    IncidentAction,
    apply_configured_incident_action,
)
from maais.operations.preflight import run_candidate_preflight
from maais.operations.process_drills import freeze_process_drill_evidence
from maais.operations.qualification import run_candidate_qualification
from maais.operations.reporting import (
    build_configured_daily_report,
    write_daily_report_bundle,
)
from maais.operations.restores import restore_configured_database
from maais.operations.soak_readiness import (
    build_configured_soak_readiness,
    write_soak_readiness_bundle,
)
from maais.operations.verification import verify_configured_ledger
from maais.platform.candidate import build_candidate_descriptor, write_candidate_descriptor


def _localhost_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonempty(value: str) -> str:
    if not value or value != value.strip():
        raise argparse.ArgumentTypeError("value must be nonempty and trimmed")
    return value


def _clean_source_assertion(value: str) -> bool:
    if value != "true":
        raise argparse.ArgumentTypeError("source-clean must be exactly true")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maais")
    commands = parser.add_subparsers(dest="command", required=True)
    candidate = commands.add_parser(
        "candidate-descriptor",
        help="derive and write the canonical secret-free cloud candidate identity",
    )
    candidate.add_argument("--repository", type=Path, required=True)
    candidate.add_argument("--dashboard-dir", type=Path, required=True)
    candidate.add_argument("--git-sha", required=True)
    candidate.add_argument("--source-clean", type=_clean_source_assertion, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser(
        "prepare-paper-live",
        help="preflight public venues and write an immutable paper manifest",
    )
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--name", required=True)
    prepare.add_argument("--repository", type=Path, default=Path.cwd())
    prepare.add_argument("--force", action="store_true")
    run = commands.add_parser(
        "paper-live",
        help="run a keyless local paper worker from an immutable manifest",
    )
    run.add_argument("--manifest", type=Path, required=True)
    mission_control = commands.add_parser(
        "mission-control",
        help="serve the local paper-trading dashboard and queued control API",
    )
    mission_control.add_argument("--port", type=_localhost_port, default=8000)
    commands.add_parser(
        "database-identity",
        help="report the configured PostgreSQL cluster identity",
    )
    commands.add_parser(
        "verify-ledger",
        help="read-only verification of event, projection, and account consistency",
    )
    report = commands.add_parser(
        "daily-report",
        help="freeze an auditable Berlin-calendar-day paper report bundle",
    )
    report.add_argument("--experiment", type=UUID, required=True)
    report.add_argument("--date", dest="report_date", type=_date, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument(
        "--allow-partial",
        action="store_true",
        help="allow a partial current-day bundle for explicit stop evidence only",
    )
    report.add_argument(
        "--resume-existing",
        action="store_true",
        help="reuse the unique verified complete bundle after an interrupted daily close",
    )
    daily_supervisor = commands.add_parser(
        "daily-supervisor",
        help="automatically close each completed Berlin day for the active paper run",
    )
    daily_supervisor.add_argument(
        "--state",
        type=Path,
        default=Path("artifacts/run-state/current.json"),
    )
    daily_supervisor.add_argument(
        "--close-script",
        type=Path,
        default=Path("scripts/daily-paper-ops.sh"),
    )
    daily_supervisor.add_argument("--poll-seconds", type=_positive_int, default=30)
    final_report = commands.add_parser(
        "final-report",
        help="verify and aggregate exactly seven immutable Berlin-day report bundles",
    )
    final_report.add_argument("--experiment", type=UUID, required=True)
    final_report.add_argument("--start-date", type=_date, required=True)
    final_report.add_argument("--reports", type=Path, required=True)
    final_report.add_argument("--output", type=Path, required=True)
    backup = commands.add_parser(
        "backup",
        help="verify and create an immutable local PostgreSQL backup bundle",
    )
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser(
        "restore",
        help="restore a verified backup into a new suffix-constrained database",
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target-database", required=True)
    restore.add_argument("--confirm-target", required=True)
    restore.add_argument("--output", type=Path, required=True)
    qualify = commands.add_parser(
        "qualify-candidate",
        help="run and freeze every quality gate for the exact clean candidate commit",
    )
    qualify.add_argument("--repository", type=Path, default=Path.cwd())
    qualify.add_argument("--output", type=Path, required=True)
    process_drills = commands.add_parser(
        "process-drill-verdict",
        help="verify and freeze disposable dashboard and worker recovery evidence",
    )
    process_drills.add_argument("--manifest", type=Path, required=True)
    process_drills.add_argument("--repository", type=Path, default=Path.cwd())
    process_drills.add_argument("--dashboard-baseline", type=Path, required=True)
    process_drills.add_argument("--dashboard-recovery", type=Path, required=True)
    process_drills.add_argument("--dashboard-after", type=Path, required=True)
    process_drills.add_argument("--worker-baseline", type=Path, required=True)
    process_drills.add_argument("--worker-recovery", type=Path, required=True)
    process_drills.add_argument("--worker-after", type=Path, required=True)
    process_drills.add_argument("--daily-close", type=Path, required=True)
    process_drills.add_argument("--output", type=Path, required=True)
    preflight = commands.add_parser(
        "preflight",
        help="evaluate all local gates for an official timed paper candidate",
    )
    preflight.add_argument("--manifest", type=Path, required=True)
    preflight.add_argument("--restore-verification", type=Path, required=True)
    preflight.add_argument("--qualification", type=Path, required=True)
    preflight.add_argument("--soak-readiness", type=Path)
    preflight.add_argument(
        "--run-purpose",
        choices=("process_drill", "soak", "seven_day"),
        default="seven_day",
    )
    preflight.add_argument("--process-drills", type=Path)
    preflight.add_argument("--repository", type=Path, default=Path.cwd())
    preflight.add_argument("--dashboard-dir", type=Path, default=Path("dashboard/dist"))
    preflight.add_argument("--minimum-free-gb", type=_positive_int, default=20)
    health = commands.add_parser(
        "health",
        help="verify ledger, runtime lease, cursor freshness, incidents, and controls",
    )
    health.add_argument("--experiment", type=UUID, required=True)
    health.add_argument("--maximum-lag-seconds", type=_positive_int, default=180)
    health.add_argument("--allow-stopped", action="store_true")
    health.add_argument("--alert", action="store_true")
    soak_verdict = commands.add_parser(
        "soak-verdict",
        help="write the immutable fail-closed verdict for an official 24-hour soak",
    )
    soak_verdict.add_argument("--experiment", type=UUID, required=True)
    soak_verdict.add_argument(
        "--state",
        type=Path,
        default=Path("artifacts/run-state/current.json"),
    )
    soak_verdict.add_argument("--repository", type=Path, default=Path.cwd())
    soak_verdict.add_argument("--output", type=Path, required=True)
    soak_verdict.add_argument("--maximum-lag-seconds", type=_positive_int, default=180)
    acknowledge = commands.add_parser(
        "acknowledge-incident",
        help="append an audited operator acknowledgement to an incident",
    )
    acknowledge.add_argument("--experiment", type=UUID, required=True)
    acknowledge.add_argument("--incident", type=UUID, required=True)
    acknowledge.add_argument("--actor", type=_nonempty, required=True)
    resolve = commands.add_parser(
        "resolve-incident",
        help="append an explicitly reviewed incident resolution",
    )
    resolve.add_argument("--experiment", type=UUID, required=True)
    resolve.add_argument("--incident", type=UUID, required=True)
    resolve.add_argument("--actor", type=_nonempty, required=True)
    resolve.add_argument("--resolution", type=_nonempty, required=True)
    resolve.add_argument("--confirm-reviewed", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "candidate-descriptor":
        descriptor = build_candidate_descriptor(
            repository_root=arguments.repository,
            dashboard_dist=arguments.dashboard_dir,
            git_sha=arguments.git_sha,
            source_clean=arguments.source_clean,
        )
        write_candidate_descriptor(descriptor, arguments.output)
        print(
            json.dumps(
                {
                    "descriptor_hash": descriptor.descriptor_hash,
                    "path": str(arguments.output),
                },
                sort_keys=True,
            )
        )
        return 0
    settings = get_settings()
    configure_logging(settings.log_level, settings.is_production)
    if arguments.command == "prepare-paper-live":
        manifest = asyncio.run(
            prepare_live_manifest_file(
                repository_root=arguments.repository,
                output=arguments.output,
                name=arguments.name,
                overwrite=arguments.force,
            )
        )
        print(f"prepared paper manifest {manifest.experiment_id} at {arguments.output}")
        return 0
    if arguments.command == "mission-control":
        import uvicorn

        uvicorn.run(
            "maais.api.app:app",
            host="127.0.0.1",
            port=arguments.port,
            log_config=None,
        )
        return 0
    if arguments.command == "verify-ledger":
        result = asyncio.run(verify_configured_ledger())
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] is True else 1
    if arguments.command == "database-identity":
        result = asyncio.run(collect_configured_database_identity())
        print(json.dumps(result, sort_keys=True))
        return 0
    if arguments.command == "daily-report":
        if arguments.resume_existing:
            existing = resolve_existing_daily_report_bundle(
                arguments.output,
                expected_date=arguments.report_date,
                experiment_id=arguments.experiment,
                generated_at=datetime.now(timezone.utc),
            )
            if existing is not None:
                print(json.dumps(existing, sort_keys=True))
                return 0
        report = asyncio.run(
            build_configured_daily_report(arguments.experiment, arguments.report_date)
        )
        if report.get("complete_day") is not True and not arguments.allow_partial:
            raise ValueError(
                "Berlin day is incomplete; wait until the calendar day has ended or use "
                "--allow-partial only for explicit stop evidence"
            )
        paths = write_daily_report_bundle(report, arguments.output)
        print(
            json.dumps(
                {
                    "report_id": report["report_id"],
                    "directory": str(paths.directory),
                    "json": str(paths.json_path),
                    "markdown": str(paths.markdown_path),
                    "decisions_csv": str(paths.decisions_csv_path),
                    "decisions_parquet": str(paths.decisions_parquet_path),
                    "execution_csv": str(paths.execution_csv_path),
                    "execution_parquet": str(paths.execution_parquet_path),
                    "bundle_manifest": str(paths.manifest_path),
                    "resumed": False,
                },
                sort_keys=True,
            )
        )
        reconciliation = report["reconciliation"]
        if not isinstance(reconciliation, dict):
            raise TypeError("daily report reconciliation must be an object")
        return 0 if reconciliation["ledger_ok"] is True else 1
    if arguments.command == "daily-supervisor":
        try:
            supervise_daily_closes(
                state_path=arguments.state,
                close_script=arguments.close_script,
                poll_seconds=arguments.poll_seconds,
            )
        except KeyboardInterrupt:
            pass
        return 0
    if arguments.command == "final-report":
        report = build_final_report_from_bundles(
            arguments.reports,
            experiment_id=arguments.experiment,
            start_date=arguments.start_date,
            days=7,
            generated_at=datetime.now(timezone.utc),
        )
        paths = write_final_report_bundle(report, arguments.output)
        print(
            json.dumps(
                {
                    "report_id": report["report_id"],
                    "directory": str(paths.directory),
                    "json": str(paths.json_path),
                    "markdown": str(paths.markdown_path),
                    "daily_reports_csv": str(paths.daily_reports_csv_path),
                    "bundle_manifest": str(paths.manifest_path),
                },
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "backup":
        paths = asyncio.run(backup_configured_database(arguments.output))
        print(
            json.dumps(
                {
                    "directory": str(paths.directory),
                    "dump": str(paths.dump_path),
                    "manifest": str(paths.manifest_path),
                },
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "restore":
        paths, passed = asyncio.run(
            restore_configured_database(
                arguments.backup,
                target_database=arguments.target_database,
                confirmation=arguments.confirm_target,
                output_directory=arguments.output,
            )
        )
        print(
            json.dumps(
                {
                    "passed": passed,
                    "directory": str(paths.directory),
                    "verification": str(paths.result_path),
                },
                sort_keys=True,
            )
        )
        return 0 if passed else 1
    if arguments.command == "qualify-candidate":
        test_database_url = (
            os.environ.get("MAAIS_TEST_DATABASE_URL") or settings.maais_test_database_url_value
        )
        if not test_database_url:
            raise ValueError(
                "MAAIS_TEST_DATABASE_URL is required and must name a PostgreSQL _test database"
            )
        paths, report = run_candidate_qualification(
            repository_root=arguments.repository,
            output_directory=arguments.output,
            test_database_url=test_database_url,
        )
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "report_id": report["report_id"],
                    "directory": str(paths.directory),
                    "qualification": str(paths.report_path),
                    "bundle_manifest": str(paths.manifest_path),
                },
                sort_keys=True,
            )
        )
        return 0 if report["passed"] is True else 1
    if arguments.command == "process-drill-verdict":
        paths, report = freeze_process_drill_evidence(
            manifest_path=arguments.manifest,
            repository_root=arguments.repository,
            dashboard_baseline_path=arguments.dashboard_baseline,
            dashboard_recovery_path=arguments.dashboard_recovery,
            dashboard_after_path=arguments.dashboard_after,
            worker_baseline_path=arguments.worker_baseline,
            worker_recovery_path=arguments.worker_recovery,
            worker_after_path=arguments.worker_after,
            daily_close_path=arguments.daily_close,
            output_directory=arguments.output,
            generated_at=datetime.now(timezone.utc),
        )
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "report_id": report["report_id"],
                    "directory": str(paths.directory),
                    "report": str(paths.report_path),
                    "bundle_manifest": str(paths.manifest_path),
                },
                sort_keys=True,
            )
        )
        return 0 if report["passed"] is True else 1
    if arguments.command == "preflight":
        report = asyncio.run(
            run_candidate_preflight(
                manifest_path=arguments.manifest,
                restore_verification_path=arguments.restore_verification,
                qualification_directory=arguments.qualification,
                run_purpose=arguments.run_purpose,
                process_drill_directory=arguments.process_drills,
                soak_readiness_directory=arguments.soak_readiness,
                repository_root=arguments.repository,
                dashboard_directory=arguments.dashboard_dir,
                minimum_free_gb=arguments.minimum_free_gb,
            )
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["passed"] is True else 1
    if arguments.command == "health":
        report = asyncio.run(
            collect_configured_experiment_health(
                arguments.experiment,
                maximum_lag=timedelta(seconds=arguments.maximum_lag_seconds),
                allow_stopped=arguments.allow_stopped,
                send_alert=arguments.alert,
            )
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["healthy"] is True else 1
    if arguments.command == "soak-verdict":
        report = asyncio.run(
            build_configured_soak_readiness(
                experiment_id=arguments.experiment,
                state_path=arguments.state,
                repository_root=arguments.repository,
                maximum_lag=timedelta(seconds=arguments.maximum_lag_seconds),
            )
        )
        paths = write_soak_readiness_bundle(report, arguments.output)
        print(
            json.dumps(
                {
                    "passed": report["passed"],
                    "verdict": report["verdict"],
                    "report_id": report["report_id"],
                    "directory": str(paths.directory),
                    "json": str(paths.json_path),
                    "markdown": str(paths.markdown_path),
                    "bundle_manifest": str(paths.manifest_path),
                },
                sort_keys=True,
            )
        )
        return 0 if report["passed"] is True else 1
    if arguments.command in {"acknowledge-incident", "resolve-incident"}:
        resolving = arguments.command == "resolve-incident"
        result = asyncio.run(
            apply_configured_incident_action(
                experiment_id=arguments.experiment,
                incident_id=arguments.incident,
                action=(IncidentAction.RESOLVE if resolving else IncidentAction.ACKNOWLEDGE),
                actor=arguments.actor,
                resolution=arguments.resolution if resolving else None,
                operator_confirmed=arguments.confirm_reviewed if resolving else False,
            )
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    manifest = load_manifest_file(arguments.manifest)
    try:
        asyncio.run(run_live_paper_manifest(manifest, settings=settings))
    except Exception as exc:
        error = (str(exc).strip().replace("\x00", "") or "no detail")[:2000]
        print(
            json.dumps(
                {
                    "event": "paper_live_failed",
                    "level": "error",
                    "experiment_id": str(manifest.experiment_id),
                    "error_type": type(exc).__name__,
                    "error": error,
                    "live_money": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
