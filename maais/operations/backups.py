"""Fail-closed PostgreSQL backup bundles for the local paper platform."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.artifacts.models import validate_sha256
from maais.config.settings import get_settings
from maais.db.replay import verify_ledger_consistency
from maais.domain.json import content_hash, to_json_data
from maais.operations.verification import establish_read_only_snapshot, ledger_consistency_payload

UTC = timezone.utc
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class BackupMetadata:
    database_name: str
    schema_revision: str
    database_size_bytes: int
    table_counts: dict[str, int]
    ledger: dict[str, object]
    producer: BackupProducerIdentity | None = None


@dataclass(frozen=True, slots=True)
class BackupProducerIdentity:
    artifact_schema_version: Literal[1]
    environment: str
    candidate_hash: str
    experiment_id: UUID
    run_id: UUID
    operation_id: UUID
    database_system_identifier_sha256: str
    railway_deployment_id: str
    railway_replica_id: str
    railway_region: str

    def __post_init__(self) -> None:
        if self.artifact_schema_version != 1:
            raise ValueError("artifact_schema_version must be 1")
        if self.environment not in {"qualification", "production"}:
            raise ValueError("backup environment must be qualification or production")
        try:
            validate_sha256(self.candidate_hash)
        except ValueError as error:
            raise ValueError("backup candidate_hash must be lowercase SHA-256") from error
        try:
            validate_sha256(self.database_system_identifier_sha256)
        except ValueError as error:
            raise ValueError(
                "backup database_system_identifier_sha256 must be lowercase SHA-256"
            ) from error
        for name, value in (
            ("experiment_id", self.experiment_id),
            ("run_id", self.run_id),
            ("operation_id", self.operation_id),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"backup {name} must be a non-nil UUID")
        for name, value in (
            ("railway_deployment_id", self.railway_deployment_id),
            ("railway_replica_id", self.railway_replica_id),
            ("railway_region", self.railway_region),
        ):
            if not value or value != value.strip() or len(value) > 128:
                raise ValueError(f"backup {name} must be nonempty, trimmed, and bounded")

    def to_json_data(self) -> dict[str, object]:
        return {
            "artifact_schema_version": self.artifact_schema_version,
            "candidate_hash": self.candidate_hash,
            "database_system_identifier_sha256": self.database_system_identifier_sha256,
            "environment": self.environment,
            "experiment_id": str(self.experiment_id),
            "operation_id": str(self.operation_id),
            "railway_deployment_id": self.railway_deployment_id,
            "railway_region": self.railway_region,
            "railway_replica_id": self.railway_replica_id,
            "run_id": str(self.run_id),
        }

    @classmethod
    def from_json_data(cls, value: object) -> BackupProducerIdentity:
        expected = {
            "artifact_schema_version",
            "candidate_hash",
            "database_system_identifier_sha256",
            "environment",
            "experiment_id",
            "operation_id",
            "railway_deployment_id",
            "railway_region",
            "railway_replica_id",
            "run_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("backup producer identity must contain exact fields")
        if (
            type(value["artifact_schema_version"]) is not int
            or value["artifact_schema_version"] != 1
        ):
            raise ValueError("backup producer identity artifact schema is invalid")
        string_fields = expected - {"artifact_schema_version"}
        if any(type(value[field]) is not str for field in string_fields):
            raise ValueError("backup producer identity must contain string fields")
        try:
            return cls(
                artifact_schema_version=1,
                environment=value["environment"],
                candidate_hash=value["candidate_hash"],
                experiment_id=UUID(value["experiment_id"]),
                run_id=UUID(value["run_id"]),
                operation_id=UUID(value["operation_id"]),
                database_system_identifier_sha256=value["database_system_identifier_sha256"],
                railway_deployment_id=value["railway_deployment_id"],
                railway_replica_id=value["railway_replica_id"],
                railway_region=value["railway_region"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("backup producer identity is invalid") from error


@dataclass(frozen=True, slots=True)
class BackupBundlePaths:
    directory: Path
    dump_path: Path
    manifest_path: Path
    report_id: str | None = None
    bundle_manifest_path: Path | None = None


def postgres_cli_connection(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Convert a SQLAlchemy URL into password-free PostgreSQL CLI arguments."""
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("backup requires a PostgreSQL database URL")
    if not url.database:
        raise ValueError("database URL must name a database")
    arguments = [
        "--host",
        url.host or "localhost",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "postgres",
        "--dbname",
        url.database,
    ]
    environment = os.environ.copy()
    if url.password is not None:
        environment["PGPASSWORD"] = url.password
    sslmode = url.query.get("sslmode")
    if isinstance(sslmode, str):
        environment["PGSSLMODE"] = sslmode
    return arguments, environment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(
    runner: Runner,
    command: list[str],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    result = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "no diagnostic output"
        raise RuntimeError(f"{command[0]} failed: {detail}")
    return result


def create_database_backup(
    database_url: str,
    output_directory: Path,
    metadata: BackupMetadata,
    *,
    generated_at: datetime,
    runner: Runner = subprocess.run,
) -> BackupBundlePaths:
    """Create, validate, and hash one immutable PostgreSQL custom archive."""
    if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(0):
        raise ValueError("generated_at must be UTC-aware")
    connection_arguments, environment = postgres_cli_connection(database_url)
    configured_database = connection_arguments[-1]
    if configured_database != metadata.database_name:
        raise ValueError("backup metadata database does not match configured database")
    if metadata.ledger.get("ok") is not True:
        raise ValueError("ledger verification must pass before backup")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", metadata.database_name):
        raise ValueError("database name is not safe for a backup bundle path")

    inventory_payload: dict[str, object] = {
        "database": metadata.database_name,
        "schema_revision": metadata.schema_revision,
        "database_size_bytes": metadata.database_size_bytes,
        "table_counts": metadata.table_counts,
        "ledger": metadata.ledger,
    }
    if metadata.producer is not None:
        inventory_payload["producer"] = metadata.producer.to_json_data()
    inventory_hash = content_hash(inventory_payload)
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    bundle_name = f"{timestamp}-{metadata.database_name}-{inventory_hash[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / bundle_name
    if target.exists():
        raise FileExistsError(f"backup bundle already exists: {target}")

    pg_dump_version = _run_checked(
        runner,
        ["pg_dump", "--version"],
        environment=environment,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix=".maais-backup-", dir=output_directory) as temporary:
        temporary_path = Path(temporary)
        dump_path = temporary_path / "database.dump"
        manifest_path = temporary_path / "backup-manifest.json"
        bundle_manifest_path = temporary_path / "bundle-manifest.json"
        _run_checked(
            runner,
            [
                "pg_dump",
                "--format=custom",
                "--compress=9",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(dump_path),
                *connection_arguments,
            ],
            environment=environment,
        )
        if not dump_path.is_file() or dump_path.stat().st_size == 0:
            raise RuntimeError("pg_dump did not create a non-empty archive")
        _run_checked(
            runner,
            ["pg_restore", "--list", str(dump_path)],
            environment=environment,
        )
        manifest_payload: dict[str, object] = {
            "backup_schema_version": 2 if metadata.producer is not None else 1,
            "created_at": generated_at,
            "database_name": metadata.database_name,
            "schema_revision": metadata.schema_revision,
            "database_size_bytes": metadata.database_size_bytes,
            "table_counts": dict(sorted(metadata.table_counts.items())),
            "ledger": metadata.ledger,
            "inventory_hash": inventory_hash,
            "pg_dump_version": pg_dump_version,
            "dump": {
                "filename": dump_path.name,
                "bytes": dump_path.stat().st_size,
                "sha256": _sha256(dump_path),
                "format": "postgresql_custom",
                "compression": 9,
            },
        }
        report_id: str | None = None
        if metadata.producer is not None:
            producer = metadata.producer.to_json_data()
            manifest_payload.update(
                artifact_schema_version=metadata.producer.artifact_schema_version,
                producer=producer,
            )
            report_id = content_hash(to_json_data(manifest_payload))
            manifest_payload["report_id"] = report_id
        manifest = to_json_data(manifest_payload)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if report_id is not None:
            artifacts = (manifest_path, dump_path)
            bundle_manifest_path.write_text(
                json.dumps(
                    {
                        "artifact_schema_version": 1,
                        "report_id": report_id,
                        "artifacts": {
                            path.name: {
                                "bytes": path.stat().st_size,
                                "sha256": _sha256(path),
                            }
                            for path in artifacts
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        temporary_path.replace(target)
    return BackupBundlePaths(
        directory=target,
        dump_path=target / "database.dump",
        manifest_path=target / "backup-manifest.json",
        report_id=report_id,
        bundle_manifest_path=(target / "bundle-manifest.json" if report_id is not None else None),
    )


async def collect_backup_metadata(database_url: str) -> BackupMetadata:
    """Collect database inventory and ledger evidence in one read-only transaction."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await establish_read_only_snapshot(session)
                database_name = str(await session.scalar(text("SELECT current_database()")))
                schema_revision = str(
                    await session.scalar(text("SELECT version_num FROM alembic_version"))
                )
                database_size_bytes = int(
                    await session.scalar(text("SELECT pg_database_size(current_database())")) or 0
                )
                table_names = list(
                    await session.scalars(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                            "ORDER BY table_name"
                        )
                    )
                )
                table_counts: dict[str, int] = {}
                for raw_name in table_names:
                    table_name = str(raw_name)
                    quoted_name = table_name.replace('"', '""')
                    table_counts[table_name] = int(
                        await session.scalar(text(f'SELECT count(*) FROM "{quoted_name}"')) or 0
                    )
                ledger = ledger_consistency_payload(await verify_ledger_consistency(session))
        return BackupMetadata(
            database_name=database_name,
            schema_revision=schema_revision,
            database_size_bytes=database_size_bytes,
            table_counts=table_counts,
            ledger=ledger,
        )
    finally:
        await engine.dispose()


async def backup_configured_database(output_directory: Path) -> BackupBundlePaths:
    settings = get_settings()
    metadata = await collect_backup_metadata(settings.database_url_value)
    return await asyncio.to_thread(
        create_database_backup,
        settings.database_url_value,
        output_directory,
        metadata,
        generated_at=datetime.now(UTC),
    )
