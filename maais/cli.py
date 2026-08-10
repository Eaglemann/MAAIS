"""Local operator CLI for the paper worker and Mission Control."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from maais.config.artifacts import ArtifactStoreMode, ArtifactType
from maais.config.cloud import DeploymentTarget, ServiceRole
from maais.config.settings import Settings, get_settings
from maais.core.logging import configure_logging, get_logger
from maais.db.connection import get_session_factory
from maais.db.repositories.observability import ObservabilityRepository
from maais.db.repositories.platform import PlatformRepository
from maais.db.roles import load_database_role_passwords
from maais.live import (
    PaperLiveConfigurationError,
    load_manifest_file,
    prepare_live_manifest_file,
    run_live_paper_manifest,
)
from maais.observability.sentry import (
    SentryRuntime,
    capture_terminal_exception,
    flush_backend_sentry,
    initialize_backend_sentry,
)
from maais.operations.backups import backup_configured_database
from maais.operations.cloud_artifacts import (
    CloudOperationResult,
    backup_configured_cloud_database,
    close_configured_cloud_day,
    publish_configured_cloud_bundle,
    restore_configured_cloud_backup,
)
from maais.operations.cloud_evidence import CloudEvidenceSnapshot
from maais.operations.cloud_preflight import (
    evaluate_cloud_preflight,
    write_cloud_preflight_bundle,
)
from maais.operations.cloud_process_drills import (
    CloudProcessDrillSnapshot,
    evaluate_cloud_process_drills,
    write_cloud_process_drill_bundle,
)
from maais.operations.cloud_soak_readiness import (
    CloudSoakSnapshot,
    evaluate_cloud_soak_readiness,
    write_cloud_soak_readiness_bundle,
)
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
from maais.operations.migrations import initialize_database_with_url, migrate_with_url
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
from maais.operations.verification import establish_read_only_snapshot, verify_configured_ledger
from maais.orchestration.supervisor import PaperWorkerHalt
from maais.platform.candidate import build_candidate_descriptor, write_candidate_descriptor
from maais.platform.lifecycle import require_service_role
from maais.platform.runtime import verify_configured_runtime_identity
from maais.platform.services import (
    attest_cloud_migrator_service,
    ensure_cloud_migrator_candidate,
    run_cloud_operations_service,
    run_cloud_verifier_service,
    run_cloud_web_service,
    run_cloud_worker_service,
)
from maais.security.passwords import hash_operator_password

logger = get_logger(__name__)


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


def _schema_revision(value: str) -> str:
    if len(value) != 4 or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("schema revision must be four ASCII decimal digits")
    return value


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("value must be a lowercase SHA-256 digest")
    return value


def _add_cloud_verdict_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate-hash", type=_sha256, required=True)
    parser.add_argument("--run", type=UUID, required=True)
    parser.add_argument("--experiment", type=UUID, required=True)
    parser.add_argument("--manifest-hash", type=_sha256, required=True)
    parser.add_argument(
        "--environment",
        choices=("qualification", "production"),
        required=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maais")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "operator-password-hash",
        help="interactively derive an Argon2id operator hash without shell arguments",
    )
    commands.add_parser(
        "generate-secret-token",
        help="generate one high-entropy provider secret without shell arguments",
    )
    candidate = commands.add_parser(
        "candidate-descriptor",
        help="derive and write the canonical secret-free cloud candidate identity",
    )
    candidate.add_argument("--repository", type=Path, required=True)
    candidate.add_argument("--dashboard-dir", type=Path, required=True)
    candidate.add_argument("--git-sha", required=True)
    candidate.add_argument("--source-clean", type=_clean_source_assertion, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    cloud_bootstrap = commands.add_parser(
        "cloud-bootstrap-roles",
        help="initialize schema and fixed least-privilege PostgreSQL service roles",
    )
    cloud_bootstrap.add_argument("--expected-revision", type=_schema_revision, required=True)
    cloud_bootstrap.add_argument("--repository", type=Path, default=Path.cwd())
    cloud_migrate = commands.add_parser(
        "cloud-migrate",
        help="run guarded Alembic migration under the purpose-bound migrator role",
    )
    cloud_migrate.add_argument("--expected-revision", type=_schema_revision, required=True)
    cloud_migrate.add_argument("--repository", type=Path, default=Path.cwd())
    commands.add_parser(
        "cloud-web",
        help="serve authenticated Mission Control under the verified web service role",
    )
    commands.add_parser(
        "cloud-worker",
        help="run the artifact-backed paper worker under the verified worker service role",
    )
    cloud_verifier = commands.add_parser(
        "cloud-verifier",
        help="verify the read-only cloud runtime identity for one exact run",
    )
    cloud_verifier.add_argument("--run-id", type=UUID)
    cloud_identity = commands.add_parser(
        "cloud-identity",
        help="verify and register the current secret-free Railway runtime identity",
    )
    cloud_identity.add_argument("--json", action="store_true", required=True)
    cloud_publish = commands.add_parser(
        "cloud-publish",
        help="publish one locally verified bundle to both immutable cloud targets",
    )
    cloud_publish.add_argument("--run", type=UUID, required=True)
    cloud_publish.add_argument("--experiment", type=UUID, required=True)
    cloud_publish.add_argument("--date", dest="report_date", type=_date, required=True)
    cloud_publish.add_argument(
        "--type",
        dest="artifact_type",
        choices=tuple(value.value for value in ArtifactType),
        required=True,
    )
    cloud_publish.add_argument("--report-id", type=_sha256, required=True)
    cloud_publish.add_argument("--bundle", type=Path, required=True)
    cloud_backup = commands.add_parser(
        "cloud-backup",
        help="create and durably publish one cataloged cloud logical backup",
    )
    cloud_backup.add_argument("--run", type=UUID, required=True)
    cloud_backup.add_argument("--experiment", type=UUID, required=True)
    cloud_backup.add_argument("--date", dest="report_date", type=_date, required=True)
    cloud_backup.add_argument("--output", type=Path, required=True)
    cloud_restore = commands.add_parser(
        "cloud-restore-verify",
        help="restore one cataloged exact canonical backup version to the secret test target",
    )
    cloud_restore.add_argument("--artifact-record", type=UUID, required=True)
    cloud_restore.add_argument("--output", type=Path, required=True)
    cloud_daily_close = commands.add_parser(
        "cloud-daily-close",
        help="publish the exactly-once report and backup for one completed Berlin day",
    )
    cloud_daily_close.add_argument("--run", type=UUID, required=True)
    cloud_daily_close.add_argument("--experiment", type=UUID, required=True)
    cloud_daily_close.add_argument("--date", dest="report_date", type=_date, required=True)
    cloud_daily_close.add_argument("--temporary-parent", type=Path, required=True)
    cloud_operations = commands.add_parser(
        "cloud-operations",
        help="run the single-owner immutable one-minute cloud health supervisor",
    )
    cloud_operations.add_argument("--run", type=UUID)
    cloud_preflight = commands.add_parser(
        "cloud-preflight",
        help="evaluate and dual-store one frozen cloud preflight snapshot",
    )
    _add_cloud_verdict_identity_arguments(cloud_preflight)
    cloud_preflight.add_argument("--local-preflight", type=Path, required=True)
    cloud_preflight.add_argument("--snapshot", type=Path, required=True)
    cloud_preflight.add_argument("--output", type=Path, required=True)
    cloud_process_drills = commands.add_parser(
        "cloud-process-drill-verdict",
        help="evaluate and dual-store frozen, operator-triggered cloud drill observations",
    )
    _add_cloud_verdict_identity_arguments(cloud_process_drills)
    cloud_process_drills.add_argument("--snapshot", type=Path, required=True)
    cloud_process_drills.add_argument("--output", type=Path, required=True)
    cloud_soak = commands.add_parser(
        "cloud-soak-verdict",
        help="evaluate and dual-store an uninterrupted cloud soak after 24 hours",
    )
    _add_cloud_verdict_identity_arguments(cloud_soak)
    cloud_soak.add_argument("--local-soak", type=Path, required=True)
    cloud_soak.add_argument("--snapshot", type=Path, required=True)
    cloud_soak.add_argument("--output", type=Path, required=True)
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
    sentry_test = commands.add_parser(
        "sentry-test-event",
        help="emit one non-sensitive backend Sentry qualification event",
    )
    sentry_test.add_argument(
        "--state",
        type=Path,
        default=Path("artifacts/run-state/current.json"),
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
    if arguments.command == "operator-password-hash":
        return operator_password_hash_command()
    if arguments.command == "generate-secret-token":
        return generate_secret_token_command()
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
    sentry_runtime = initialize_backend_sentry(settings.observability)
    if sentry_runtime.initialization_error is not None:
        logger.error(
            "sentry_initialization_failed",
            error_code="sentry_initialization_failed",
            outcome="disabled",
        )
    if arguments.command == "sentry-test-event":
        try:
            active = _active_local_timed_run(arguments.state)
            if settings.deployment_target is DeploymentTarget.RAILWAY:
                active = active or asyncio.run(
                    _active_cloud_timed_run(settings.railway_environment_id)
                )
        except Exception:
            logger.exception(
                "sentry_test_event_refused",
                error_code="timed_run_state_invalid",
                outcome="refused",
            )
            return 1
        if active:
            logger.warning(
                "sentry_test_event_refused",
                reason_code="active_timed_run",
                outcome="refused",
            )
            return 1
        captured = sentry_runtime.capture_message(
            "maais_backend_sentry_test_event",
            event="sentry_test_event",
            outcome="qualification",
        )
        flushed = sentry_runtime.flush(timeout=5.0) if captured else False
        logger.info(
            "sentry_test_event_completed" if captured and flushed else "sentry_test_event_failed",
            error_code=("" if captured and flushed else "sentry_delivery_unconfirmed"),
            outcome=("confirmed" if captured and flushed else "unconfirmed"),
        )
        return 0 if captured and flushed else 1
    if arguments.command == "cloud-bootstrap-roles":
        revision, roles = asyncio.run(
            initialize_database_with_url(
                settings.database_url_value,
                load_database_role_passwords(os.environ),
                expected_revision=arguments.expected_revision,
                repository_root=arguments.repository,
            )
        )
        print(
            json.dumps(
                {"roles": roles, "schema_revision": revision, "live_money": False},
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "cloud-migrate":
        try:
            require_service_role(settings, ServiceRole.MIGRATOR)
            if arguments.expected_revision != settings.expected_schema_revision:
                raise ValueError("cloud migration revision differs from candidate identity")
            revision = asyncio.run(
                migrate_with_url(
                    settings.database_url_value,
                    expected_revision=arguments.expected_revision,
                    repository_root=arguments.repository,
                )
            )
            asyncio.run(ensure_cloud_migrator_candidate(settings))
            asyncio.run(attest_cloud_migrator_service(settings))
        except Exception as exc:
            return _cloud_terminal_failure("cloud_migrate", exc)
        print(
            json.dumps(
                {"schema_revision": revision, "live_money": False},
                sort_keys=True,
            )
        )
        _flush_cloud_service_shutdown("cloud_migrate", sentry_runtime)
        return 0
    if arguments.command == "cloud-web":
        try:
            asyncio.run(run_cloud_web_service(settings))
        except Exception as exc:
            return _cloud_terminal_failure("cloud_web", exc)
        print(json.dumps({"live_money": False, "status": "stopped"}, sort_keys=True))
        _flush_cloud_service_shutdown("cloud_web", sentry_runtime)
        return 0
    if arguments.command == "cloud-worker":
        try:
            asyncio.run(run_cloud_worker_service(settings))
        except Exception as exc:
            return _cloud_terminal_failure("cloud_worker", exc)
        print(json.dumps({"live_money": False, "status": "stopped"}, sort_keys=True))
        _flush_cloud_service_shutdown("cloud_worker", sentry_runtime)
        return 0
    if arguments.command == "cloud-verifier":
        try:
            run_id = arguments.run_id or settings.cloud_run_id
            if run_id is None:
                raise ValueError("cloud verifier requires MAAIS_RUN_ID")
            if settings.cloud_run_id is not None and settings.cloud_run_id != run_id:
                raise ValueError("cloud verifier run ID differs from MAAIS_RUN_ID")
            evidence = asyncio.run(run_cloud_verifier_service(settings, run_id=run_id))
        except Exception as exc:
            return _cloud_terminal_failure("cloud_verifier", exc)
        print(json.dumps(evidence.to_json_data(), sort_keys=True))
        _flush_cloud_service_shutdown("cloud_verifier", sentry_runtime)
        return 0
    if arguments.command == "cloud-identity":
        evidence = asyncio.run(verify_configured_runtime_identity(settings=settings))
        print(json.dumps(evidence.to_json_data(), sort_keys=True))
        return 0
    if arguments.command == "cloud-operations":
        try:
            run_id = arguments.run or settings.cloud_run_id
            if run_id is None:
                raise ValueError("cloud operations requires MAAIS_RUN_ID")
            asyncio.run(
                run_cloud_operations(
                    settings=settings,
                    run_id=run_id,
                    sentry_runtime=sentry_runtime,
                )
            )
        except Exception as exc:
            return _cloud_terminal_failure("cloud_operations", exc)
        print(json.dumps({"live_money": False, "status": "stopped"}, sort_keys=True))
        _flush_cloud_service_shutdown("cloud_operations", sentry_runtime)
        return 0
    if arguments.command == "cloud-preflight":
        evaluated_at = datetime.now(timezone.utc)
        snapshot = CloudEvidenceSnapshot.from_dict(_load_json_object(arguments.snapshot))
        report = evaluate_cloud_preflight(
            local_preflight=_load_json_object(arguments.local_preflight),
            snapshot=snapshot,
            expected_candidate_hash=arguments.candidate_hash,
            expected_run_id=arguments.run,
            expected_experiment_id=arguments.experiment,
            expected_manifest_hash=arguments.manifest_hash,
            expected_environment=arguments.environment,
            evaluated_at=evaluated_at,
        )
        paths = write_cloud_preflight_bundle(report, arguments.output)
        result = asyncio.run(
            _publish_cloud_verdict(
                settings=settings,
                run_id=arguments.run,
                experiment_id=arguments.experiment,
                environment=arguments.environment,
                candidate_hash=arguments.candidate_hash,
                artifact_type=ArtifactType.PREFLIGHT,
                report=report,
                bundle_directory=paths.directory,
                generated_at=evaluated_at,
            )
        )
        print(json.dumps(_cloud_verdict_output(report, paths.directory, result), sort_keys=True))
        return 0 if report["passed"] is True else 1
    if arguments.command == "cloud-process-drill-verdict":
        evaluated_at = datetime.now(timezone.utc)
        snapshot = CloudProcessDrillSnapshot.from_dict(_load_json_object(arguments.snapshot))
        report = evaluate_cloud_process_drills(
            snapshot,
            expected_candidate_hash=arguments.candidate_hash,
            expected_run_id=arguments.run,
            expected_experiment_id=arguments.experiment,
            expected_manifest_hash=arguments.manifest_hash,
            expected_environment=arguments.environment,
            evaluated_at=evaluated_at,
        )
        paths = write_cloud_process_drill_bundle(report, arguments.output)
        result = asyncio.run(
            _publish_cloud_verdict(
                settings=settings,
                run_id=arguments.run,
                experiment_id=arguments.experiment,
                environment=arguments.environment,
                candidate_hash=arguments.candidate_hash,
                artifact_type=ArtifactType.PROCESS_DRILL,
                report=report,
                bundle_directory=paths.directory,
                generated_at=evaluated_at,
            )
        )
        print(json.dumps(_cloud_verdict_output(report, paths.directory, result), sort_keys=True))
        return 0 if report["passed"] is True else 1
    if arguments.command == "cloud-soak-verdict":
        evaluated_at = datetime.now(timezone.utc)
        snapshot = CloudSoakSnapshot.from_dict(_load_json_object(arguments.snapshot))
        report = evaluate_cloud_soak_readiness(
            local_soak=_load_json_object(arguments.local_soak),
            snapshot=snapshot,
            expected_candidate_hash=arguments.candidate_hash,
            expected_run_id=arguments.run,
            expected_experiment_id=arguments.experiment,
            expected_manifest_hash=arguments.manifest_hash,
            expected_environment=arguments.environment,
            evaluated_at=evaluated_at,
        )
        paths = write_cloud_soak_readiness_bundle(report, arguments.output)
        result = asyncio.run(
            _publish_cloud_verdict(
                settings=settings,
                run_id=arguments.run,
                experiment_id=arguments.experiment,
                environment=arguments.environment,
                candidate_hash=arguments.candidate_hash,
                artifact_type=ArtifactType.SOAK_VERDICT,
                report=report,
                bundle_directory=paths.directory,
                generated_at=evaluated_at,
            )
        )
        print(json.dumps(_cloud_verdict_output(report, paths.directory, result), sort_keys=True))
        return 0 if report["passed"] is True else 1
    if arguments.command == "cloud-publish":
        result = asyncio.run(
            publish_configured_cloud_bundle(
                settings=settings,
                run_id=arguments.run,
                experiment_id=arguments.experiment,
                report_date=arguments.report_date,
                artifact_type=ArtifactType(arguments.artifact_type),
                report_id=arguments.report_id,
                bundle_directory=arguments.bundle,
            )
        )
        print(json.dumps(result.to_json_data(), sort_keys=True))
        return 0
    if arguments.command == "cloud-backup":
        result = asyncio.run(
            backup_configured_cloud_database(
                settings=settings,
                run_id=arguments.run,
                experiment_id=arguments.experiment,
                report_date=arguments.report_date,
                temporary_parent=arguments.output,
            )
        )
        print(json.dumps(result.to_json_data(), sort_keys=True))
        return 0
    if arguments.command == "cloud-restore-verify":
        result = asyncio.run(
            restore_configured_cloud_backup(
                settings=settings,
                artifact_record_id=arguments.artifact_record,
                output_directory=arguments.output,
            )
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["passed"] is True else 1
    if arguments.command == "cloud-daily-close":
        result = asyncio.run(
            close_configured_cloud_day(
                settings=settings,
                run_id=arguments.run,
                experiment_id=arguments.experiment,
                report_date=arguments.report_date,
                temporary_parent=arguments.temporary_parent,
            )
        )
        print(
            json.dumps(
                {
                    "backup_artifact_record_id": str(result.backup_record.id),
                    "operation_id": str(result.operation.id),
                    "report_artifact_record_id": str(result.report_record.id),
                    "resumed": result.resumed,
                    "status": result.operation.status.value,
                },
                sort_keys=True,
            )
        )
        return 0
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

        try:
            uvicorn.run(
                "maais.api.app:app",
                host="127.0.0.1",
                port=arguments.port,
                log_config=None,
            )
        except Exception as exc:
            logger.exception(
                "mission_control_failed",
                error_code="mission_control_unhandled_exception",
                outcome="terminated",
            )
            _capture_exception_without_suppressing_exit(
                exc,
                event="mission_control_terminal_failure",
                error_code="mission_control_unhandled_exception",
                outcome="terminated",
            )
            return 1
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
        except Exception as exc:
            logger.exception(
                "daily_supervisor_failed",
                error_code="daily_supervisor_unhandled_exception",
                outcome="terminated",
            )
            _capture_exception_without_suppressing_exit(
                exc,
                event="daily_supervisor_terminal_failure",
                error_code="daily_supervisor_unhandled_exception",
                outcome="terminated",
            )
            return 1
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
    try:
        manifest = load_manifest_file(arguments.manifest)
    except (OSError, TypeError, ValueError):
        logger.error(
            "paper_live_refused",
            error_code="paper_manifest_invalid",
            outcome="refused",
        )
        return 1
    try:
        asyncio.run(run_live_paper_manifest(manifest, settings=settings))
    except PaperLiveConfigurationError:
        logger.error(
            "paper_live_refused",
            experiment_ref=str(manifest.experiment_id),
            error_code="worker_configuration_invalid",
            outcome="refused",
        )
        return 1
    except Exception as exc:
        outcome = (
            exc.halt_persistence_outcome
            if isinstance(exc, PaperWorkerHalt)
            else "halt_persistence_unknown"
        )
        logger.exception(
            "paper_live_failed",
            experiment_ref=str(manifest.experiment_id),
            error_code="worker_unhandled_exception",
            outcome=outcome,
        )
        _capture_worker_failure_without_suppressing_exit(exc, outcome=outcome)
        return 1
    return 0


def _active_local_timed_run(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("timed run state must be a JSON object")
    run_purpose = value.get("run_purpose")
    if run_purpose not in {"process_drill", "soak", "seven_day"}:
        raise ValueError("current run state has an invalid run purpose")
    if value.get("stopped_at") is not None:
        return False
    if not isinstance(value.get("experiment_id"), str) or not value["experiment_id"]:
        raise ValueError("current run state requires an experiment ID")
    if not isinstance(value.get("started_at"), str) or not value["started_at"]:
        raise ValueError("current run state requires a start time")
    return True


def _load_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"cloud evidence input must be a JSON object: {path.name}")
    return value


async def _publish_cloud_verdict(
    *,
    settings: Settings,
    run_id: UUID,
    experiment_id: UUID,
    environment: str,
    candidate_hash: str,
    artifact_type: ArtifactType,
    report: Mapping[str, object],
    bundle_directory: Path,
    generated_at: datetime,
) -> CloudOperationResult:
    if settings.environment != environment:
        raise ValueError("cloud verdict environment differs from configured environment")
    if settings.cloud_run_id != run_id:
        raise ValueError("cloud verdict run differs from configured MAAIS_RUN_ID")
    runtime = await verify_configured_runtime_identity(settings=settings, run_id=run_id)
    if runtime.identity.candidate_hash != candidate_hash:
        raise ValueError("cloud verdict candidate differs from the deployed runtime")
    report_id = report.get("report_id")
    if not isinstance(report_id, str):
        raise ValueError("cloud verdict report is missing its immutable identity")
    return await publish_configured_cloud_bundle(
        settings=settings,
        run_id=run_id,
        experiment_id=experiment_id,
        report_date=generated_at.date(),
        artifact_type=artifact_type,
        report_id=report_id,
        bundle_directory=bundle_directory,
    )


def _cloud_verdict_output(
    report: Mapping[str, object],
    directory: Path,
    result: CloudOperationResult,
) -> dict[str, object]:
    return {
        "passed": report.get("passed") is True,
        "report_id": report.get("report_id"),
        "directory": str(directory),
        "publication": result.to_json_data(),
        "safety": {"paper_trading_only": True, "live_money": False},
    }


async def run_cloud_operations(
    *,
    settings: Settings,
    run_id: UUID,
    sentry_runtime: SentryRuntime,
) -> None:
    _validate_cloud_operations_settings(settings)
    await run_cloud_operations_service(
        settings.model_copy(update={"cloud_run_id": run_id}),
        sentry_runtime=sentry_runtime,
    )


def _validate_cloud_operations_settings(settings: Settings) -> None:
    artifacts = settings.artifacts
    observability = settings.observability
    if settings.deployment_target is not DeploymentTarget.RAILWAY:
        raise ValueError("cloud operations requires a Railway deployment")
    if settings.service_role is not ServiceRole.OPERATIONS:
        raise ValueError("cloud operations requires the operations service role")
    if (
        artifacts.mode is not ArtifactStoreMode.DUAL_S3
        or not artifacts.replica_configured
        or not artifacts.canonical_configured
        or artifacts.canonical_object_lock_required is not True
    ):
        raise ValueError("cloud operations requires complete dual-store WORM settings")
    if set(observability.cron_monitor_slugs) != {"daily_close", "backup", "evidence"}:
        raise ValueError("cloud operations requires all Sentry Cron monitors")
    if not observability.backend_dsn_value:
        raise ValueError("cloud operations requires backend Sentry")


def _cloud_terminal_failure(command: str, exception: BaseException) -> int:
    error_code = f"{command}_unhandled_exception"
    logger.exception(
        f"{command}_failed",
        error_code=error_code,
        outcome="terminated",
    )
    _capture_exception_without_suppressing_exit(
        exception,
        event=f"{command}_terminal_failure",
        error_code=error_code,
        outcome="terminated",
    )
    return 1


def _flush_cloud_service_shutdown(command: str, sentry_runtime: SentryRuntime) -> None:
    """Best-effort bounded telemetry drain after durable service stop evidence."""

    logger.info(
        f"{command}_stopped",
        outcome="stopped",
    )
    if sentry_runtime.enabled and not sentry_runtime.flush(timeout=5.0):
        logger.error(
            f"{command}_sentry_flush_unconfirmed",
            error_code="sentry_flush_unconfirmed",
            outcome="unconfirmed",
        )
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            continue


async def _active_cloud_timed_run(railway_environment_id: str) -> bool:
    session_factory = get_session_factory()
    async with session_factory() as session:
        async with session.begin():
            await establish_read_only_snapshot(session)
            active = await PlatformRepository(
                session,
                ObservabilityRepository(session),
            ).get_active_run(railway_environment_id)
    return active is not None


def _capture_worker_failure_without_suppressing_exit(
    exception: BaseException,
    *,
    outcome: str,
) -> None:
    original = exception
    persistence_error: BaseException | None = None
    if isinstance(exception, PaperWorkerHalt):
        original = exception.original_exception or exception
        persistence_error = exception.persistence_error
    _capture_exception_without_suppressing_exit(
        original,
        event="worker_terminal_failure",
        error_code="worker_unhandled_exception",
        outcome=outcome,
        tags={"phase": "primary"},
    )
    if persistence_error is not None:
        _capture_exception_without_suppressing_exit(
            persistence_error,
            event="worker_terminal_failure",
            error_code="worker_halt_persistence_failed",
            outcome=outcome,
            tags={"phase": "halt_persistence"},
        )


def _capture_exception_without_suppressing_exit(
    exception: BaseException,
    *,
    event: str,
    error_code: str,
    outcome: str,
    tags: dict[str, object] | None = None,
) -> None:
    try:
        capture_terminal_exception(
            exception,
            event=event,
            error_code=error_code,
            outcome=outcome,
            tags=tags,
        )
        flush_backend_sentry(timeout=5.0)
    except Exception:
        return


def operator_password_hash_command(
    *,
    reader: Callable[[str], str] | None = None,
    output: Callable[[str], object] | None = None,
    input_is_tty: bool | None = None,
) -> int:
    if (sys.stdin.isatty() if input_is_tty is None else input_is_tty) is not True:
        raise RuntimeError("operator password hashing requires an interactive TTY")
    read_secret = reader or getpass.getpass
    write = output or (lambda value: print(value, end=""))
    passphrase = read_secret("Operator passphrase: ")
    confirmation = read_secret("Confirm operator passphrase: ")
    if not secrets.compare_digest(passphrase, confirmation):
        raise ValueError("operator passphrase confirmation does not match")
    write(hash_operator_password(passphrase) + "\n")
    return 0


def generate_secret_token_command(
    *,
    output: Callable[[str], object] | None = None,
) -> int:
    write = output or (lambda value: print(value, end=""))
    write(secrets.token_urlsafe(32) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
