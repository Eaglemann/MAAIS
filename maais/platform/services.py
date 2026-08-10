"""Strict Railway service entrypoint helpers and artifact-backed worker inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import uvicorn
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.api.health import (
    CloudEndpointReader,
    CloudEndpointSnapshot,
    DatabaseCloudEndpointReader,
    UnavailableCloudEndpointReader,
)
from maais.artifacts.bundles import BUNDLE_MANIFEST_NAME
from maais.artifacts.configured import build_configured_artifact_reader
from maais.artifacts.models import ArtifactRecord, ArtifactType, StoredArtifact
from maais.artifacts.store import ArtifactStore
from maais.config.cloud import ServiceRole
from maais.config.settings import Settings
from maais.core.logging import get_logger
from maais.db.models.platform import PlatformCandidateModel
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.artifacts import ArtifactRepository
from maais.db.repositories.observability import ObservabilityRepository
from maais.db.unit_of_work import UnitOfWork
from maais.domain.json import content_hash
from maais.experiments.manifest import ExperimentManifest
from maais.live import run_live_paper_manifest
from maais.monitoring.alerting import SentryCronReporter
from maais.observability.sentry import SentryRuntime
from maais.operations.cloud_health import (
    CloudHealthEvaluator,
    DatabaseCloudHealthSnapshotReader,
)
from maais.operations.health_supervisor import HealthSupervisor, PostgresHealthOwnership
from maais.operations.verification import establish_read_only_snapshot
from maais.platform.identity import CandidateDescriptor
from maais.platform.lifecycle import (
    DatabaseServiceLifecycleBackend,
    ServiceLifecycle,
    cloud_service_lifecycle,
    require_service_role,
)
from maais.platform.registry import CandidateStatus, PlatformCandidate
from maais.platform.runtime import (
    RuntimeIdentityError,
    RuntimeIdentityEvidence,
    load_embedded_candidate_descriptor,
)

_MANIFEST_DOCUMENT_NAME = "manifest.json"
_MAX_MANIFEST_BUNDLE_BYTES = 4 * 1024 * 1024
_ENVELOPE_KEYS = frozenset(
    {
        "candidate_hash",
        "manifest",
        "manifest_hash",
        "report_id",
    }
)

logger = get_logger(__name__)


class ManifestArtifactIntegrityError(RuntimeError):
    """The frozen worker manifest failed one or more identity checks."""


class ManifestArtifactCatalog(Protocol):
    async def get_record(self, record_id: UUID) -> ArtifactRecord: ...


class ReadyLifecycle(Protocol):
    @property
    def ready(self) -> bool: ...


class LifecycleCloudEndpointReader:
    """Keep public readiness generic until all role startup checks pass."""

    def __init__(
        self,
        *,
        lifecycle: ReadyLifecycle,
        delegate: CloudEndpointReader,
    ) -> None:
        self._lifecycle = lifecycle
        self._delegate = delegate
        self._unavailable = UnavailableCloudEndpointReader()

    async def readiness(self) -> bool:
        return self._lifecycle.ready and await self._delegate.readiness()

    async def monitor(self) -> CloudEndpointSnapshot:
        if not self._lifecycle.ready:
            return await self._unavailable.monitor()
        return await self._delegate.monitor()


class LifecycleUvicornServer(uvicorn.Server):
    """Let the shared lifecycle own SIGTERM/SIGINT and bounded shutdown."""

    @contextmanager
    def capture_signals(self):
        yield


def build_cloud_web_server(application: Any, *, port: int) -> LifecycleUvicornServer:
    if not 1 <= port <= 65_535:
        raise ValueError("cloud web port must be between 1 and 65535")
    return LifecycleUvicornServer(
        uvicorn.Config(
            application,
            host="::",
            port=port,
            workers=1,
            proxy_headers=False,
            forwarded_allow_ips="",
            access_log=False,
            server_header=False,
            timeout_graceful_shutdown=20,
        )
    )


async def run_cloud_web_service(
    settings: Settings,
    *,
    backend_factory: Callable[[Settings], DatabaseServiceLifecycleBackend] = (
        DatabaseServiceLifecycleBackend
    ),
    lifecycle_factory: Callable[..., AbstractAsyncContextManager[ServiceLifecycle]] = (
        cloud_service_lifecycle
    ),
    app_factory: Callable[..., Any] | None = None,
    server_builder: Callable[..., LifecycleUvicornServer] = build_cloud_web_server,
) -> None:
    require_service_role(settings, ServiceRole.WEB)
    if app_factory is None:
        from maais.api.app import create_app

        app_factory = create_app
    backend = backend_factory(settings)
    async with lifecycle_factory(
        role=ServiceRole.WEB,
        run_id=None,
        settings=settings,
        backend=backend,
    ) as lifecycle:
        delegate = DatabaseCloudEndpointReader(
            session_factory=backend.session_factory,
            expected_schema_revision=lifecycle.evidence.schema_revision,
            expected_candidate_hash=lifecycle.evidence.identity.candidate_hash,
            railway_environment_id=lifecycle.evidence.identity.environment_id,
            boot_verified=True,
        )
        application = app_factory(
            session_factory=backend.session_factory,
            dashboard_dir=Path("/app/dashboard"),
            security_settings=settings.security,
            cloud_health_reader=LifecycleCloudEndpointReader(
                lifecycle=lifecycle,
                delegate=delegate,
            ),
        )
        server = server_builder(application, port=settings.port)
        remove_signal_handlers = lifecycle.install_signal_handlers()
        try:
            await _serve_web_until_stopped(server, lifecycle)
        finally:
            remove_signal_handlers()


async def run_cloud_operations_service(
    settings: Settings,
    *,
    sentry_runtime: SentryRuntime,
    backend_factory: Callable[[Settings], DatabaseServiceLifecycleBackend] = (
        DatabaseServiceLifecycleBackend
    ),
    lifecycle_factory: Callable[..., AbstractAsyncContextManager[ServiceLifecycle]] = (
        cloud_service_lifecycle
    ),
) -> None:
    require_service_role(settings, ServiceRole.OPERATIONS)
    run_id = settings.cloud_run_id
    if run_id is None:
        raise ValueError("cloud operations requires MAAIS_RUN_ID")
    backend = backend_factory(settings)
    async with lifecycle_factory(
        role=ServiceRole.OPERATIONS,
        run_id=run_id,
        settings=settings,
        backend=backend,
    ) as lifecycle:
        async with backend.session_factory() as session:
            async with session.begin():
                await establish_read_only_snapshot(session)
                audit = await ObservabilityRepository(session).verify_audit_chain()
        if not audit.ok:
            raise RuntimeError("cloud operations startup audit chain is invalid")

        reporter = SentryCronReporter(
            runtime=sentry_runtime,
            monitor_slugs=settings.observability.cron_monitor_slugs,
        )
        reader = DatabaseCloudHealthSnapshotReader(
            session_factory=backend.session_factory,
            runtime_evidence=lifecycle.evidence,
            environment=settings.environment,
            sentry_delivery_confirmed=lambda: reporter.last_delivery_confirmed,
        )
        evaluator = _ReadyHealthEvaluator(
            delegate=CloudHealthEvaluator(
                uow_factory=UnitOfWork(backend.session_factory),
                snapshot_reader=reader,
                service_boot_id=lifecycle.evidence.identity.boot_id,
            ),
            lifecycle=lifecycle,
        )
        supervisor = HealthSupervisor(
            evaluator=evaluator,
            run_id=run_id,
            ownership=PostgresHealthOwnership(backend.engine, run_id=run_id),
        )
        remove_signal_handlers = lifecycle.install_signal_handlers()
        try:
            await _run_health_until_stopped(supervisor, lifecycle)
        finally:
            remove_signal_handlers()


async def run_cloud_verifier_service(
    settings: Settings,
    *,
    run_id: UUID,
    backend_factory: Callable[[Settings], DatabaseServiceLifecycleBackend] = (
        DatabaseServiceLifecycleBackend
    ),
    lifecycle_factory: Callable[..., AbstractAsyncContextManager[ServiceLifecycle]] = (
        cloud_service_lifecycle
    ),
) -> RuntimeIdentityEvidence:
    require_service_role(settings, ServiceRole.VERIFIER)
    if run_id.int == 0:
        raise ValueError("cloud verifier requires a non-nil run identifier")
    backend = backend_factory(settings)
    async with lifecycle_factory(
        role=ServiceRole.VERIFIER,
        run_id=run_id,
        settings=settings,
        backend=backend,
    ) as lifecycle:
        async with backend.session_factory() as session:
            async with session.begin():
                await establish_read_only_snapshot(session)
                audit = await ObservabilityRepository(session).verify_audit_chain()
                ledger = await verify_ledger_consistency(session)
        if not audit.ok or not ledger.ok:
            raise RuntimeError("cloud verifier startup evidence is invalid")
        lifecycle.mark_ready()
        return lifecycle.evidence


async def attest_cloud_migrator_service(
    settings: Settings,
    *,
    backend_factory: Callable[[Settings], DatabaseServiceLifecycleBackend] = (
        DatabaseServiceLifecycleBackend
    ),
    lifecycle_factory: Callable[..., AbstractAsyncContextManager[ServiceLifecycle]] = (
        cloud_service_lifecycle
    ),
) -> RuntimeIdentityEvidence:
    require_service_role(settings, ServiceRole.MIGRATOR)
    backend = backend_factory(settings)
    async with lifecycle_factory(
        role=ServiceRole.MIGRATOR,
        run_id=None,
        settings=settings,
        backend=backend,
    ) as lifecycle:
        lifecycle.mark_ready()
        return lifecycle.evidence


async def ensure_cloud_migrator_candidate(
    settings: Settings,
    *,
    backend_factory: Callable[[Settings], DatabaseServiceLifecycleBackend] = (
        DatabaseServiceLifecycleBackend
    ),
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CandidateDescriptor:
    """Catalog the embedded candidate without rewriting its first-deploy evidence."""

    require_service_role(settings, ServiceRole.MIGRATOR)
    descriptor = load_embedded_candidate_descriptor(settings)
    if descriptor.git_sha != settings.railway_git_commit_sha:
        raise RuntimeIdentityError("candidate Git commit does not match Railway deployment")
    if descriptor.schema_revision != settings.expected_schema_revision:
        raise RuntimeIdentityError("candidate schema revision does not match Railway settings")
    candidate = PlatformCandidate.register(
        descriptor,
        creator_deployment_id=settings.railway_deployment_id,
        registered_at=clock(),
    )
    backend = backend_factory(settings)
    try:
        async with backend.session_factory() as session:
            async with session.begin():
                created_hash = await session.scalar(
                    insert(PlatformCandidateModel)
                    .values(
                        descriptor_hash=descriptor.descriptor_hash,
                        git_sha=descriptor.git_sha,
                        schema_revision=descriptor.schema_revision,
                        descriptor_json=descriptor.to_json_data(),
                        status=candidate.status.value,
                        creator_deployment_id=candidate.creator_deployment_id,
                        registered_at=candidate.registered_at,
                        qualifying_at=None,
                        qualified_at=None,
                        qualification_evidence_hash=None,
                    )
                    .on_conflict_do_nothing()
                    .returning(PlatformCandidateModel.descriptor_hash)
                )
                row = await session.get(
                    PlatformCandidateModel,
                    descriptor.descriptor_hash,
                )
                if row is None:
                    raise RuntimeIdentityError("candidate catalog write was not durable")
                try:
                    existing = PlatformCandidate(
                        descriptor=CandidateDescriptor.from_json_data(row.descriptor_json),
                        status=CandidateStatus(row.status),
                        creator_deployment_id=row.creator_deployment_id,
                        registered_at=row.registered_at,
                        qualifying_at=row.qualifying_at,
                        qualified_at=row.qualified_at,
                        qualification_evidence_hash=row.qualification_evidence_hash,
                    )
                except ValueError as exc:
                    raise RuntimeIdentityError("catalogued candidate evidence is invalid") from exc
                if (
                    existing.descriptor != descriptor
                    or row.git_sha != descriptor.git_sha
                    or row.schema_revision != descriptor.schema_revision
                ):
                    raise RuntimeIdentityError(
                        "catalogued candidate conflicts with embedded identity"
                    )
    finally:
        await backend.close()
    logger.info(
        "cloud_migrator_candidate_catalogued",
        candidate_hash=descriptor.descriptor_hash,
        deployment_id=settings.railway_deployment_id,
        outcome="created" if created_hash is not None else "reused",
    )
    return descriptor


class _ReadyHealthEvaluator:
    def __init__(self, *, delegate: CloudHealthEvaluator, lifecycle: ServiceLifecycle) -> None:
        self._delegate = delegate
        self._lifecycle = lifecycle
        self._ready = False

    async def evaluate(self, run_id: UUID, checked_at):
        result = await self._delegate.evaluate(run_id, checked_at)
        if not self._ready:
            self._lifecycle.mark_ready()
            self._ready = True
        return result


async def _run_health_until_stopped(
    supervisor: HealthSupervisor,
    lifecycle: ServiceLifecycle,
) -> None:
    supervisor_task = asyncio.create_task(
        supervisor.run(),
        name="cloud_operations_health_supervisor",
    )
    stop_task = asyncio.create_task(
        lifecycle.wait_until_stopped(),
        name="cloud_operations_lifecycle_stop",
    )
    try:
        done, _ = await asyncio.wait(
            (supervisor_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if supervisor_task in done:
            await supervisor_task
            if not lifecycle.stop_requested.is_set():
                raise RuntimeError("cloud operations supervisor stopped unexpectedly")
        else:
            supervisor.request_stop()
            await asyncio.wait_for(supervisor_task, timeout=20)
            await stop_task
    finally:
        supervisor.request_stop()
        for task in (supervisor_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(supervisor_task, stop_task, return_exceptions=True)


async def _serve_web_until_stopped(
    server: LifecycleUvicornServer,
    lifecycle: ServiceLifecycle,
) -> None:
    server_task = asyncio.create_task(server.serve(), name="cloud_web_uvicorn")
    stop_task: asyncio.Task[None] | None = None
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("cloud web server stopped before startup completed")
            await asyncio.sleep(0.01)
        lifecycle.mark_ready()
        stop_task = asyncio.create_task(
            lifecycle.wait_until_stopped(),
            name="cloud_web_lifecycle_stop",
        )
        done, _ = await asyncio.wait(
            (server_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if server_task in done:
            await server_task
            if not lifecycle.stop_requested.is_set():
                raise RuntimeError("cloud web server stopped unexpectedly")
        else:
            server.should_exit = True
            try:
                await asyncio.wait_for(server_task, timeout=25)
            except TimeoutError:
                server.force_exit = True
                raise RuntimeError("cloud web server exceeded graceful shutdown deadline") from None
            await stop_task
    finally:
        server.should_exit = True
        for task in (server_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (server_task, stop_task) if task is not None),
            return_exceptions=True,
        )


class DatabaseManifestArtifactCatalog:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_record(self, record_id: UUID) -> ArtifactRecord:
        async with self._session_factory() as session:
            async with session.begin():
                await establish_read_only_snapshot(session)
                return await ArtifactRepository(
                    session,
                    ObservabilityRepository(session),
                ).get_record(record_id)


async def load_configured_manifest_artifact(
    *,
    settings: Settings,
    artifact_record_id: UUID,
    expected_run_id: UUID,
    expected_candidate: CandidateDescriptor,
) -> ExperimentManifest:
    runtime = build_configured_artifact_reader(settings)
    try:
        return await load_verified_manifest_artifact(
            catalog=DatabaseManifestArtifactCatalog(
                async_sessionmaker(runtime.engine, expire_on_commit=False)
            ),
            canonical_store=runtime.canonical_store,
            artifact_record_id=artifact_record_id,
            expected_run_id=expected_run_id,
            expected_candidate=expected_candidate,
        )
    finally:
        await runtime.close()


async def run_cloud_worker_service(
    settings: Settings,
    *,
    lifecycle_factory: Callable[..., AbstractAsyncContextManager[ServiceLifecycle]] = (
        cloud_service_lifecycle
    ),
    manifest_loader: Callable[..., Awaitable[ExperimentManifest]] = (
        load_configured_manifest_artifact
    ),
    worker_runner: Callable[..., Awaitable[None]] = run_live_paper_manifest,
    candidate_loader: Callable[[Settings], CandidateDescriptor] = (
        load_embedded_candidate_descriptor
    ),
) -> None:
    """Start one artifact-bound worker that remains standby pending its audited command."""

    require_service_role(settings, ServiceRole.WORKER)
    run_id = settings.cloud_run_id
    artifact_record_id = settings.manifest_artifact_id
    if run_id is None:
        raise ValueError("cloud worker requires MAAIS_RUN_ID")
    if artifact_record_id is None:
        raise ValueError("cloud worker requires MAAIS_MANIFEST_ARTIFACT_ID")
    async with lifecycle_factory(
        role=ServiceRole.WORKER,
        run_id=run_id,
        settings=settings,
    ) as lifecycle:
        candidate = candidate_loader(settings)
        if candidate.descriptor_hash != lifecycle.evidence.identity.candidate_hash:
            raise ManifestArtifactIntegrityError("embedded_candidate_identity_mismatch")
        manifest = await manifest_loader(
            settings=settings,
            artifact_record_id=artifact_record_id,
            expected_run_id=run_id,
            expected_candidate=candidate,
        )

        def status_writer(status: dict[str, object]) -> None:
            event = str(status.get("event", "worker_status"))
            state = str(status.get("worker_state", "unknown"))
            if event == "paper_live_started":
                if state not in {"standby", "running"}:
                    raise RuntimeError("cloud worker startup state is invalid")
                lifecycle.mark_ready()
            logger.info(
                event,
                experiment_ref=str(manifest.experiment_id),
                outcome=state,
            )

        remove_signal_handlers = lifecycle.install_signal_handlers()
        try:
            await worker_runner(
                manifest,
                settings=settings,
                stop_event=lifecycle.stop_requested,
                status_writer=status_writer,
                worker_id=lifecycle.evidence.identity.boot_id,
                platform_run_id=run_id,
            )
        finally:
            remove_signal_handlers()


async def load_verified_manifest_artifact(
    *,
    catalog: ManifestArtifactCatalog,
    canonical_store: ArtifactStore,
    artifact_record_id: UUID,
    expected_run_id: UUID,
    expected_candidate: CandidateDescriptor,
) -> ExperimentManifest:
    """Read one exact canonical manifest bundle after validating every identity layer."""

    if artifact_record_id.int == 0 or expected_run_id.int == 0:
        raise ValueError("manifest artifact and run identifiers cannot be nil")
    try:
        record = await catalog.get_record(artifact_record_id)
        _validate_record(record, artifact_record_id, expected_run_id, expected_candidate)
        objects, relative_inventory = await _read_exact_bundle(record, canonical_store)
        _validate_bundle_identity(record, relative_inventory)
        bundle_manifest = _canonical_json(
            objects[BUNDLE_MANIFEST_NAME],
            label="manifest bundle inventory",
        )
        envelope = _canonical_json(
            objects[_MANIFEST_DOCUMENT_NAME],
            label="manifest artifact",
        )
        _validate_bundle_manifest(bundle_manifest, record, relative_inventory)
        return _manifest_from_envelope(envelope, record, expected_candidate)
    except ManifestArtifactIntegrityError:
        raise
    except Exception as error:
        raise ManifestArtifactIntegrityError("manifest_artifact_verification_failed") from error


def _validate_record(
    record: ArtifactRecord,
    artifact_record_id: UUID,
    expected_run_id: UUID,
    expected_candidate: CandidateDescriptor,
) -> None:
    if content_hash(record.content_payload()) != record.catalog_content_hash:
        raise ManifestArtifactIntegrityError("manifest_catalog_content_hash_mismatch")
    if record.id != artifact_record_id:
        raise ManifestArtifactIntegrityError("manifest_catalog_record_identity_mismatch")
    if record.artifact_type is not ArtifactType.MANIFEST:
        raise ManifestArtifactIntegrityError("manifest_catalog_type_mismatch")
    if record.run_id != expected_run_id:
        raise ManifestArtifactIntegrityError("manifest_catalog_run_mismatch")
    if record.candidate_hash != expected_candidate.descriptor_hash:
        raise ManifestArtifactIntegrityError("manifest_catalog_candidate_mismatch")


async def _read_exact_bundle(
    record: ArtifactRecord,
    canonical_store: ArtifactStore,
) -> tuple[dict[str, bytes], tuple[tuple[str, StoredArtifact], ...]]:
    prefix = "/".join(
        (
            "maais",
            record.environment,
            record.candidate_hash,
            str(record.experiment_id),
            record.artifact_type.value,
            record.report_id,
            "",
        )
    )
    if record.size_bytes > _MAX_MANIFEST_BUNDLE_BYTES:
        raise ManifestArtifactIntegrityError("manifest_bundle_exceeds_size_limit")

    objects: dict[str, bytes] = {}
    relative_inventory: list[tuple[str, StoredArtifact]] = []
    observed_total = 0
    for expected in record.canonical_inventory:
        if expected.version_id is None:
            raise ManifestArtifactIntegrityError("manifest_canonical_version_missing")
        if not expected.key.startswith(prefix):
            raise ManifestArtifactIntegrityError("manifest_canonical_key_mismatch")
        relative_path = expected.key[len(prefix) :]
        if not relative_path or relative_path in objects:
            raise ManifestArtifactIntegrityError("manifest_canonical_inventory_invalid")
        observed = await canonical_store.head(
            expected.key,
            version_id=expected.version_id,
        )
        if observed != expected:
            raise ManifestArtifactIntegrityError("manifest_canonical_head_mismatch")
        chunks: list[bytes] = []
        size = 0
        digest = hashlib.sha256()
        async for chunk in canonical_store.read_chunks(
            expected.key,
            version_id=expected.version_id,
        ):
            if not isinstance(chunk, bytes):
                raise ManifestArtifactIntegrityError("manifest_canonical_chunk_invalid")
            size += len(chunk)
            observed_total += len(chunk)
            if size > expected.size_bytes or observed_total > _MAX_MANIFEST_BUNDLE_BYTES:
                raise ManifestArtifactIntegrityError("manifest_canonical_size_mismatch")
            digest.update(chunk)
            chunks.append(chunk)
        if size != expected.size_bytes or digest.hexdigest() != expected.sha256:
            raise ManifestArtifactIntegrityError("manifest_canonical_content_mismatch")
        objects[relative_path] = b"".join(chunks)
        relative_inventory.append((relative_path, expected))
    if observed_total != record.size_bytes:
        raise ManifestArtifactIntegrityError("manifest_bundle_size_mismatch")
    if set(objects) != {BUNDLE_MANIFEST_NAME, _MANIFEST_DOCUMENT_NAME}:
        raise ManifestArtifactIntegrityError("manifest_bundle_inventory_mismatch")
    return objects, tuple(sorted(relative_inventory))


def _validate_bundle_identity(
    record: ArtifactRecord,
    relative_inventory: tuple[tuple[str, StoredArtifact], ...],
) -> None:
    observed = content_hash(
        [
            {
                "content_type": item.content_type,
                "relative_path": relative_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for relative_path, item in relative_inventory
        ]
    )
    if observed != record.bundle_content_hash:
        raise ManifestArtifactIntegrityError("manifest_bundle_content_hash_mismatch")


def _validate_bundle_manifest(
    value: object,
    record: ArtifactRecord,
    relative_inventory: tuple[tuple[str, StoredArtifact], ...],
) -> None:
    if not isinstance(value, dict) or value.get("report_id") != record.report_id:
        raise ManifestArtifactIntegrityError("manifest_bundle_report_identity_mismatch")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {_MANIFEST_DOCUMENT_NAME}:
        raise ManifestArtifactIntegrityError("manifest_bundle_artifact_inventory_mismatch")
    expected = dict(relative_inventory)[_MANIFEST_DOCUMENT_NAME]
    manifest_identity = artifacts[_MANIFEST_DOCUMENT_NAME]
    if not isinstance(manifest_identity, dict) or manifest_identity != {
        "bytes": expected.size_bytes,
        "sha256": expected.sha256,
    }:
        raise ManifestArtifactIntegrityError("manifest_bundle_artifact_identity_mismatch")


def _manifest_from_envelope(
    value: object,
    record: ArtifactRecord,
    expected_candidate: CandidateDescriptor,
) -> ExperimentManifest:
    if not isinstance(value, dict) or set(value) != _ENVELOPE_KEYS:
        raise ManifestArtifactIntegrityError("manifest_envelope_shape_invalid")
    raw_manifest = value.get("manifest")
    if not isinstance(raw_manifest, Mapping):
        raise ManifestArtifactIntegrityError("manifest_payload_invalid")
    manifest = ExperimentManifest.from_dict(raw_manifest)
    if (
        value.get("report_id") != record.report_id
        or value.get("manifest_hash") != manifest.manifest_hash
        or record.report_id != manifest.manifest_hash
        or record.experiment_id != manifest.experiment_id
        or value.get("candidate_hash") != expected_candidate.descriptor_hash
    ):
        raise ManifestArtifactIntegrityError("manifest_envelope_identity_mismatch")
    agent_hashes = {
        entry.agent_name: entry.implementation_hash for entry in manifest.agent_versions
    }
    if (
        manifest.git_sha != expected_candidate.git_sha
        or manifest.lock_hash != expected_candidate.uv_lock_sha256
        or manifest.schema_revision != expected_candidate.schema_revision
        or agent_hashes != dict(expected_candidate.agent_implementation_hashes)
    ):
        raise ManifestArtifactIntegrityError("manifest_candidate_identity_mismatch")
    return manifest


def _canonical_json(raw: bytes, *, label: str) -> object:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise ManifestArtifactIntegrityError(f"{label}_invalid_json") from error
    canonical = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if raw != canonical:
        raise ManifestArtifactIntegrityError(f"{label}_not_canonical")
    return value
