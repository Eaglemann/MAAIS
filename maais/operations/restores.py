"""Safe restore drills for immutable MAAIS PostgreSQL backup bundles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy.engine import make_url

from maais.artifacts.bundles import validate_bundle
from maais.artifacts.models import ArtifactRecord, ArtifactType, StoredArtifact
from maais.artifacts.store import ArtifactStore, ArtifactVerificationError
from maais.config.settings import get_settings
from maais.db.unit_of_work import UnitOfWork
from maais.domain.json import content_hash, to_json_data
from maais.operations.backups import (
    BackupMetadata,
    BackupProducerIdentity,
    collect_backup_metadata,
    postgres_cli_connection,
)

UTC = timezone.utc
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class VerifiedBackup:
    directory: Path
    dump_path: Path
    manifest_path: Path
    manifest_hash: str
    source_database: str
    schema_revision: str
    table_counts: dict[str, int]
    dump_hash: str
    report_id: str | None = None
    producer: BackupProducerIdentity | None = None


@dataclass(frozen=True, slots=True)
class RestoreVerificationPaths:
    directory: Path
    result_path: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_restore_target(
    source_database: str,
    target_database: str,
    confirmation: str,
) -> None:
    if confirmation != target_database:
        raise ValueError("restore target confirmation must exactly match target database")
    if target_database == source_database:
        raise ValueError("restore target must differ from the source database")
    if not target_database.endswith(("_restore", "_test")):
        raise ValueError("restore target must end with _restore or _test")
    if not re.fullmatch(r"[A-Za-z0-9_]+", target_database):
        raise ValueError("restore target contains unsafe characters")


def validate_cloud_restore_target_url(source_database: str, target_database_url: str) -> str:
    url = make_url(target_database_url)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise ValueError("cloud restore target must be a PostgreSQL database URL")
    target_database = url.database
    if target_database == source_database:
        raise ValueError("cloud restore target must differ from the source database")
    if not target_database.endswith("_restore_test"):
        raise ValueError("cloud restore target must end with _restore_test")
    if not re.fullmatch(r"[A-Za-z0-9_]+", target_database):
        raise ValueError("cloud restore target contains unsafe characters")
    return target_database


def load_verified_backup(directory: Path) -> VerifiedBackup:
    directory = directory.resolve()
    manifest_path = directory / "backup-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"backup manifest not found: {manifest_path}")
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw_manifest, dict) or raw_manifest.get("backup_schema_version") not in {
        1,
        2,
    }:
        raise ValueError("unsupported backup manifest schema")
    schema_version = int(raw_manifest["backup_schema_version"])
    if (
        not isinstance(raw_manifest.get("ledger"), dict)
        or raw_manifest["ledger"].get("ok") is not True
    ):
        raise ValueError("backup manifest ledger verification did not pass")
    source_database = raw_manifest.get("database_name")
    schema_revision = raw_manifest.get("schema_revision")
    table_counts = raw_manifest.get("table_counts")
    dump = raw_manifest.get("dump")
    if not isinstance(source_database, str) or not source_database:
        raise ValueError("backup manifest database_name is invalid")
    if not isinstance(schema_revision, str) or not schema_revision:
        raise ValueError("backup manifest schema_revision is invalid")
    if not isinstance(table_counts, dict) or any(
        not isinstance(key, str) or not isinstance(value, int) or value < 0
        for key, value in table_counts.items()
    ):
        raise ValueError("backup manifest table_counts are invalid")
    if not isinstance(dump, dict):
        raise ValueError("backup manifest dump metadata is invalid")
    filename = dump.get("filename")
    expected_bytes = dump.get("bytes")
    expected_hash = dump.get("sha256")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or not isinstance(expected_bytes, int)
        or expected_bytes <= 0
        or not isinstance(expected_hash, str)
        or len(expected_hash) != 64
    ):
        raise ValueError("backup dump identity is invalid")
    dump_path = directory / filename
    if not dump_path.is_file():
        raise FileNotFoundError(f"backup dump not found: {dump_path}")
    if dump_path.stat().st_size != expected_bytes:
        raise ValueError("backup dump byte size does not match manifest")
    actual_hash = _sha256(dump_path)
    if actual_hash != expected_hash:
        raise ValueError("backup dump SHA-256 does not match manifest")
    report_id: str | None = None
    producer: BackupProducerIdentity | None = None
    if schema_version == 2:
        raw_report_id = raw_manifest.get("report_id")
        raw_producer = raw_manifest.get("producer")
        if (
            raw_manifest.get("artifact_schema_version") != 1
            or not isinstance(raw_report_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_report_id)
            or not isinstance(raw_producer, dict)
        ):
            raise ValueError("cloud backup producer identity is invalid")
        content_payload = dict(raw_manifest)
        del content_payload["report_id"]
        if content_hash(content_payload) != raw_report_id:
            raise ValueError("cloud backup report identity does not match its manifest")
        validate_bundle(directory, expected_report_id=raw_report_id)
        report_id = raw_report_id
        producer = BackupProducerIdentity.from_json_data(raw_producer)
    return VerifiedBackup(
        directory=directory,
        dump_path=dump_path,
        manifest_path=manifest_path,
        manifest_hash=_sha256(manifest_path),
        source_database=source_database,
        schema_revision=schema_revision,
        table_counts=cast(dict[str, int], table_counts),
        dump_hash=actual_hash,
        report_id=report_id,
        producer=producer,
    )


def _run_checked(
    runner: Runner,
    command: list[str],
    *,
    environment: dict[str, str],
) -> None:
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


def restore_archive(
    database_url: str,
    backup: VerifiedBackup,
    *,
    target_database: str,
    confirmation: str,
    runner: Runner = subprocess.run,
) -> None:
    """Create a new suffix-constrained database and restore one verified archive."""
    connection_arguments, environment = postgres_cli_connection(database_url)
    configured_source = connection_arguments[-1]
    if configured_source != backup.source_database:
        raise ValueError("configured source database does not match backup manifest")
    validate_restore_target(backup.source_database, target_database, confirmation)
    common_arguments = connection_arguments[:-2]
    _run_checked(
        runner,
        [
            "createdb",
            *common_arguments,
            "--maintenance-db",
            backup.source_database,
            target_database,
        ],
        environment=environment,
    )
    _run_checked(
        runner,
        [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            *common_arguments,
            "--dbname",
            target_database,
            str(backup.dump_path),
        ],
        environment=environment,
    )


def restore_archive_to_target_url(
    target_database_url: str,
    backup: VerifiedBackup,
    *,
    runner: Runner = subprocess.run,
) -> None:
    """Restore to a newly created database named by a secret target URL."""
    target_database = validate_cloud_restore_target_url(
        backup.source_database,
        target_database_url,
    )
    target_arguments, environment = postgres_cli_connection(target_database_url)
    common_arguments = target_arguments[:-2]
    _run_checked(
        runner,
        [
            "createdb",
            *common_arguments,
            "--maintenance-db",
            "postgres",
            target_database,
        ],
        environment=environment,
    )
    _run_checked(
        runner,
        [
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            *common_arguments,
            "--dbname",
            target_database,
            str(backup.dump_path),
        ],
        environment=environment,
    )


def restored_database_url(database_url: str, target_database: str) -> str:
    source_database = make_url(database_url).database or ""
    validate_restore_target(source_database, target_database, target_database)
    return (
        make_url(database_url).set(database=target_database).render_as_string(hide_password=False)
    )


def write_restore_verification(
    backup: VerifiedBackup,
    restored: BackupMetadata,
    output_directory: Path,
    *,
    generated_at: datetime,
    backup_manifest_evidence: dict[str, object] | None = None,
) -> tuple[RestoreVerificationPaths, bool]:
    if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(0):
        raise ValueError("generated_at must be UTC-aware")
    validate_restore_target(backup.source_database, restored.database_name, restored.database_name)
    schema_match = restored.schema_revision == backup.schema_revision
    table_counts_match = restored.table_counts == backup.table_counts
    missing_or_changed = {
        table_name: {
            "backup": backup.table_counts.get(table_name),
            "restored": restored.table_counts.get(table_name),
        }
        for table_name in sorted(set(backup.table_counts) | set(restored.table_counts))
        if backup.table_counts.get(table_name) != restored.table_counts.get(table_name)
    }
    passed = schema_match and table_counts_match and restored.ledger.get("ok") is True
    result = to_json_data(
        {
            "restore_verification_schema_version": 1,
            "verified_at": generated_at,
            "passed": passed,
            "source_database": backup.source_database,
            "target_database": restored.database_name,
            "backup_manifest": backup_manifest_evidence
            or {
                "path": str(backup.manifest_path),
                "sha256": backup.manifest_hash,
            },
            "dump_sha256": backup.dump_hash,
            "schema_revision": {
                "backup": backup.schema_revision,
                "restored": restored.schema_revision,
            },
            "schema_revision_match": schema_match,
            "table_counts_match": table_counts_match,
            "table_count_differences": missing_or_changed,
            "ledger": restored.ledger,
            "restored_database_size_bytes": restored.database_size_bytes,
        }
    )
    verification_id = content_hash(result)
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    bundle_name = f"{timestamp}-{restored.database_name}-{verification_id[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / bundle_name
    if target.exists():
        raise FileExistsError(f"restore verification already exists: {target}")
    with tempfile.TemporaryDirectory(prefix=".maais-restore-", dir=output_directory) as temporary:
        temporary_path = Path(temporary)
        result_path = temporary_path / "restore-verification.json"
        if not isinstance(result, dict):
            raise TypeError("restore verification must be a JSON object")
        result["verification_id"] = verification_id
        result_path.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(target)
    return RestoreVerificationPaths(target, target / "restore-verification.json"), passed


async def restore_canonical_backup_artifact(
    *,
    uow_factory: UnitOfWork,
    canonical_store: ArtifactStore,
    artifact_record_id: UUID,
    target_database_url: str,
    output_directory: Path,
    runner: Runner = subprocess.run,
    metadata_collector: Callable[[str], Awaitable[BackupMetadata]] = collect_backup_metadata,
    now: Callable[[], datetime] | None = None,
) -> tuple[RestoreVerificationPaths, bool]:
    """Download one cataloged canonical backup version and restore only to a fresh target."""
    observed_now = (now or (lambda: datetime.now(UTC)))()
    if observed_now.tzinfo is not UTC:
        raise ValueError("cloud restore clock must use the UTC timezone")
    async with uow_factory.begin() as uow:
        record = await uow.artifacts.get_record(artifact_record_id)
    if record.artifact_type is not ArtifactType.LOGICAL_BACKUP:
        raise ValueError("cloud restore requires a logical_backup artifact record")
    capabilities = await canonical_store.capabilities()
    if not (
        capabilities.exact_version_reads
        and capabilities.versioning_enabled
        and capabilities.object_lock_enabled
        and capabilities.compliance_retention_supported
    ):
        raise ArtifactVerificationError("canonical restore store lacks WORM capabilities")
    with tempfile.TemporaryDirectory(prefix="maais-cloud-restore-") as temporary:
        root = Path(temporary).resolve(strict=True)
        os.chmod(root, 0o700)
        manifest_object: StoredArtifact | None = None
        for stored in record.canonical_inventory:
            relative_path = _canonical_relative_path(record, stored)
            if relative_path == "backup-manifest.json":
                manifest_object = stored
            await _download_exact_object(
                canonical_store,
                capabilities.store_name,
                stored,
                root=root,
                destination=root / relative_path,
                observed_now=observed_now,
            )
        backup = await asyncio.to_thread(load_verified_backup, root)
        if backup.report_id != record.report_id:
            raise ArtifactVerificationError("downloaded backup identity differs from catalog")
        if backup.producer is None or not _backup_producer_matches_record(
            backup.producer,
            record,
        ):
            raise ArtifactVerificationError("backup producer identity differs from catalog")
        target_database = validate_cloud_restore_target_url(
            backup.source_database,
            target_database_url,
        )
        await asyncio.to_thread(
            restore_archive_to_target_url,
            target_database_url,
            backup,
            runner=runner,
        )
        restored = await metadata_collector(target_database_url)
        if restored.database_name != target_database:
            raise ValueError("restored database identity differs from the target URL")
        if manifest_object is None:
            raise ArtifactVerificationError("canonical backup manifest object is missing")
        return write_restore_verification(
            backup,
            restored,
            output_directory,
            generated_at=observed_now,
            backup_manifest_evidence={
                "artifact_record_id": str(record.id),
                "key": manifest_object.key,
                "sha256": manifest_object.sha256,
                "version_id": manifest_object.version_id,
            },
        )


def _canonical_relative_path(record: ArtifactRecord, stored: StoredArtifact) -> str:
    prefix = (
        f"maais/{record.environment}/{record.candidate_hash}/{record.experiment_id}/"
        f"{record.artifact_type.value}/{record.report_id}/"
    )
    if not stored.key.startswith(prefix):
        raise ArtifactVerificationError("canonical object key differs from catalog identity")
    relative_path = stored.key[len(prefix) :]
    if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
        raise ArtifactVerificationError("canonical object relative path is unsafe")
    return relative_path


def _backup_producer_matches_record(
    producer: BackupProducerIdentity,
    record: ArtifactRecord,
) -> bool:
    return (
        producer.environment == record.environment
        and producer.candidate_hash == record.candidate_hash
        and producer.experiment_id == record.experiment_id
        and producer.run_id == record.run_id
        and producer.operation_id == record.operation_id
        and producer.railway_deployment_id == record.producing_deployment_id
    )


async def _download_exact_object(
    store: ArtifactStore,
    expected_store_name: str,
    expected: StoredArtifact,
    *,
    root: Path,
    destination: Path,
    observed_now: datetime,
) -> None:
    if (
        expected.store_name != expected_store_name
        or expected.version_id is None
        or expected.retention.retain_until <= observed_now
    ):
        raise ArtifactVerificationError("canonical object lacks current exact retention evidence")
    actual = await store.head(expected.key, version_id=expected.version_id)
    if actual != expected:
        raise ArtifactVerificationError("canonical object metadata differs from catalog")
    _make_owner_only_parents(destination.parent, root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        async for chunk in store.read_chunks(expected.key, version_id=expected.version_id):
            if not isinstance(chunk, bytes):
                raise ArtifactVerificationError("canonical object stream returned non-bytes")
            digest.update(chunk)
            size_bytes += len(chunk)
            await asyncio.to_thread(_write_all, descriptor, chunk)
        await asyncio.to_thread(os.fsync, descriptor)
    finally:
        os.close(descriptor)
    os.chmod(destination, 0o600)
    if size_bytes != expected.size_bytes or digest.hexdigest() != expected.sha256:
        raise ArtifactVerificationError("canonical object bytes differ from catalog")


def _make_owner_only_parents(directory: Path, root: Path) -> None:
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise ArtifactVerificationError(
            "canonical object destination escapes restore root"
        ) from error
    current = root
    os.chmod(current, 0o700)
    for part in relative.parts:
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        os.chmod(current, 0o700)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


async def restore_configured_database(
    backup_directory: Path,
    *,
    target_database: str,
    confirmation: str,
    output_directory: Path,
) -> tuple[RestoreVerificationPaths, bool]:
    settings = get_settings()
    backup = load_verified_backup(backup_directory)
    await asyncio.to_thread(
        restore_archive,
        settings.database_url_value,
        backup,
        target_database=target_database,
        confirmation=confirmation,
    )
    restored_url = restored_database_url(settings.database_url_value, target_database)
    restored = await collect_backup_metadata(restored_url)
    return write_restore_verification(
        backup,
        restored,
        output_directory,
        generated_at=datetime.now(UTC),
    )
