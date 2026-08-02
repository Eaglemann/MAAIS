"""Small dependency-free operator CLI for MAAIS."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from maais.config.settings import get_settings
from maais.core.logging import configure_logging
from maais.live import (
    load_manifest_file,
    prepare_live_manifest_file,
    run_live_paper_manifest,
)


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
    manifest = load_manifest_file(arguments.manifest)
    asyncio.run(run_live_paper_manifest(manifest, settings=settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
