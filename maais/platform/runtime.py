"""Fail-closed Railway runtime identity verification and boot registration."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from maais.config.cloud import (
    DATABASE_ROLE_BY_SERVICE,
    EU_WEST_RAILWAY_REGION,
    DeploymentTarget,
)
from maais.config.settings import Settings
from maais.platform.identity import CandidateDescriptor, RailwayRuntimeIdentity


class RuntimeIdentityError(RuntimeError):
    """A cloud process cannot prove its immutable runtime identity."""


@dataclass(frozen=True, slots=True)
class ProcessBootIdentity:
    boot_id: UUID
    started_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeDatabaseIdentity:
    current_user: str
    schema_revision: str
    system_identifier: str


@dataclass(frozen=True, slots=True)
class RuntimeIdentityEvidence:
    identity: RailwayRuntimeIdentity
    schema_revision: str
    database_system_identifier_sha256: str

    def __post_init__(self) -> None:
        if (
            len(self.schema_revision) != 4
            or not self.schema_revision.isascii()
            or not self.schema_revision.isdecimal()
        ):
            raise ValueError("runtime evidence schema revision must be four ASCII digits")
        if len(self.database_system_identifier_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.database_system_identifier_sha256
        ):
            raise ValueError("database system identifier hash must be lowercase SHA-256")

    def to_json_data(self) -> dict[str, str]:
        return {
            "boot_id": str(self.identity.boot_id),
            "candidate_hash": self.identity.candidate_hash,
            "database_system_identifier_sha256": self.database_system_identifier_sha256,
            "deployment_id": self.identity.deployment_id,
            "region": self.identity.region,
            "replica_id": self.identity.replica_id,
            "role": self.identity.service_role.value,
            "schema_revision": self.schema_revision,
        }


_PROCESS_BOOT = ProcessBootIdentity(
    boot_id=uuid4(),
    started_at=datetime.now(timezone.utc),
)


def process_boot_identity() -> ProcessBootIdentity:
    """Return the one boot identity generated when this process imported the module."""

    return _PROCESS_BOOT


def build_runtime_identity_evidence(
    *,
    settings: Settings,
    descriptor: CandidateDescriptor,
    descriptor_from_image: CandidateDescriptor,
    database: RuntimeDatabaseIdentity,
    boot_id: UUID,
    started_at: datetime,
) -> RuntimeIdentityEvidence:
    if settings.deployment_target is not DeploymentTarget.RAILWAY:
        raise RuntimeIdentityError("cloud runtime identity requires a Railway deployment")
    if settings.service_role is None:
        raise RuntimeIdentityError("cloud runtime identity is missing its service role")
    if descriptor != descriptor_from_image:
        raise RuntimeIdentityError("candidate descriptor does not match the embedded image")
    if descriptor.git_sha != settings.railway_git_commit_sha:
        raise RuntimeIdentityError("candidate Git commit does not match Railway deployment")
    if settings.expected_railway_region != EU_WEST_RAILWAY_REGION:
        raise RuntimeIdentityError("expected Railway region is not the frozen EU West region")
    if settings.railway_region != settings.expected_railway_region:
        raise RuntimeIdentityError("unexpected Railway replica region")
    if (
        descriptor.schema_revision != settings.expected_schema_revision
        or database.schema_revision != settings.expected_schema_revision
    ):
        raise RuntimeIdentityError(
            "database, settings, and candidate schema identities do not match"
        )
    expected_role = DATABASE_ROLE_BY_SERVICE[settings.service_role]
    if settings.database_role_name != expected_role or database.current_user != expected_role:
        raise RuntimeIdentityError(
            "connected database role does not match the configured service role"
        )
    if (
        not database.system_identifier
        or len(database.system_identifier) > 32
        or not database.system_identifier.isascii()
        or not database.system_identifier.isdecimal()
    ):
        raise RuntimeIdentityError("database system identifier is invalid")

    identity = RailwayRuntimeIdentity(
        project_id=settings.railway_project_id,
        environment_id=settings.railway_environment_id,
        service_id=settings.railway_service_id,
        deployment_id=settings.railway_deployment_id,
        snapshot_id=settings.railway_snapshot_id,
        replica_id=settings.railway_replica_id,
        region=settings.railway_region,
        service_role=settings.service_role,
        boot_id=boot_id,
        candidate_hash=descriptor.descriptor_hash,
        started_at=started_at,
    )
    return RuntimeIdentityEvidence(
        identity=identity,
        schema_revision=database.schema_revision,
        database_system_identifier_sha256=hashlib.sha256(
            database.system_identifier.encode("ascii")
        ).hexdigest(),
    )


async def verify_and_register_runtime_evidence(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    descriptor: CandidateDescriptor,
    boot_id: UUID,
    started_at: datetime,
    run_id: UUID | None,
) -> RuntimeIdentityEvidence:
    descriptor_from_image = _load_embedded_descriptor(settings)
    database = await _collect_runtime_database_identity(session_factory)
    evidence = build_runtime_identity_evidence(
        settings=settings,
        descriptor=descriptor,
        descriptor_from_image=descriptor_from_image,
        database=database,
        boot_id=boot_id,
        started_at=started_at,
    )
    await _register_runtime_identity(session_factory, evidence.identity, run_id=run_id)
    return evidence


async def verify_and_register_runtime(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    descriptor: CandidateDescriptor,
    boot_id: UUID,
    started_at: datetime,
    run_id: UUID | None,
) -> RailwayRuntimeIdentity:
    evidence = await verify_and_register_runtime_evidence(
        settings=settings,
        session_factory=session_factory,
        descriptor=descriptor,
        boot_id=boot_id,
        started_at=started_at,
        run_id=run_id,
    )
    return evidence.identity


async def verify_configured_runtime_identity(
    *,
    settings: Settings,
    run_id: UUID | None = None,
) -> RuntimeIdentityEvidence:
    descriptor = _load_embedded_descriptor(settings)
    process = process_boot_identity()
    engine = create_async_engine(
        settings.database_url_value,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    try:
        return await verify_and_register_runtime_evidence(
            settings=settings,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            descriptor=descriptor,
            boot_id=process.boot_id,
            started_at=process.started_at,
            run_id=run_id,
        )
    finally:
        await engine.dispose()


def _load_embedded_descriptor(settings: Settings) -> CandidateDescriptor:
    path = settings.candidate_descriptor_path
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeIdentityError("embedded candidate descriptor is not readable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeIdentityError("embedded candidate descriptor must be a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise RuntimeIdentityError("embedded candidate descriptor must not be group/world writable")
    try:
        return CandidateDescriptor.from_path(path)
    except ValueError as exc:
        raise RuntimeIdentityError("embedded candidate descriptor is invalid") from exc


async def _collect_runtime_database_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> RuntimeDatabaseIdentity:
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT current_user AS current_user, "
                                "(SELECT version_num FROM public.alembic_version) "
                                "AS schema_revision, "
                                "system_identifier::text AS system_identifier "
                                "FROM pg_catalog.pg_control_system()"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
    except SQLAlchemyError as exc:
        raise RuntimeIdentityError("database runtime identity query failed") from exc
    return RuntimeDatabaseIdentity(
        current_user=str(row["current_user"]),
        schema_revision=str(row["schema_revision"]),
        system_identifier=str(row["system_identifier"]),
    )


async def _register_runtime_identity(
    session_factory: async_sessionmaker[AsyncSession],
    identity: RailwayRuntimeIdentity,
    *,
    run_id: UUID | None,
) -> None:
    try:
        async with session_factory() as session:
            async with session.begin():
                # Verifier defaults to read-only. Its direct DML remains denied, while this
                # purpose-bound SECURITY DEFINER gateway is the sole write it may request.
                await session.execute(text("SET TRANSACTION READ WRITE"))
                registered = await session.scalar(
                    text(
                        "SELECT public.maais_register_service_instance("
                        ":boot_id, :run_id, :project_id, :environment_id, :service_id, "
                        ":deployment_id, :snapshot_id, :replica_id, :region, :service_role, "
                        ":candidate_hash, CAST(:runtime_identity AS jsonb), :started_at, "
                        ":first_seen_at)"
                    ),
                    {
                        "boot_id": identity.boot_id,
                        "run_id": run_id,
                        "project_id": identity.project_id,
                        "environment_id": identity.environment_id,
                        "service_id": identity.service_id,
                        "deployment_id": identity.deployment_id,
                        "snapshot_id": identity.snapshot_id,
                        "replica_id": identity.replica_id,
                        "region": identity.region,
                        "service_role": identity.service_role.value,
                        "candidate_hash": identity.candidate_hash,
                        "runtime_identity": json.dumps(
                            identity.to_json_data(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        "started_at": identity.started_at,
                        "first_seen_at": identity.started_at,
                    },
                )
                if registered != identity.boot_id:
                    raise RuntimeIdentityError("service boot registration returned wrong identity")
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise RuntimeIdentityError("service boot identity conflicts") from exc
        raise RuntimeIdentityError("service boot registration failed") from exc
