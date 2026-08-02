"""Local operator CLI for the paper worker and Mission Control."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import date
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
from maais.operations.reporting import (
    build_configured_daily_report,
    write_daily_report_bundle,
)
from maais.operations.verification import verify_configured_ledger


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maais")
    commands = parser.add_subparsers(dest="command", required=True)
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
        help="serve the read-only local paper-trading dashboard API",
    )
    mission_control.add_argument("--port", type=_localhost_port, default=8000)
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
    backup = commands.add_parser(
        "backup",
        help="verify and create an immutable local PostgreSQL backup bundle",
    )
    backup.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
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
    if arguments.command == "daily-report":
        report = asyncio.run(
            build_configured_daily_report(arguments.experiment, arguments.report_date)
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
                },
                sort_keys=True,
            )
        )
        reconciliation = report["reconciliation"]
        if not isinstance(reconciliation, dict):
            raise TypeError("daily report reconciliation must be an object")
        return 0 if reconciliation["ledger_ok"] is True else 1
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
    manifest = load_manifest_file(arguments.manifest)
    asyncio.run(run_live_paper_manifest(manifest, settings=settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
