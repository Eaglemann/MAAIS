from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from maais.artifacts.models import (
    GENESIS_EVIDENCE_HASH,
    ArtifactRecord,
    ArtifactType,
    RetentionMode,
    RetentionRequest,
    StoredArtifact,
    artifact_key,
)
from maais.cli import build_parser, main
from maais.config.cloud import ServiceRole
from maais.config.settings import Settings
from maais.domain.json import content_hash
from maais.experiments.manifest import ExperimentManifest
from maais.observability.sentry import SentryRuntime
from maais.platform.identity import CandidateDescriptor
from maais.platform.lifecycle import ServiceRoleMismatch
from maais.platform.runtime import RuntimeIdentityEvidence
from maais.platform.services import (
    LifecycleCloudEndpointReader,
    ManifestArtifactIntegrityError,
    attest_cloud_migrator_service,
    build_cloud_web_server,
    load_verified_manifest_artifact,
    run_cloud_operations_service,
    run_cloud_verifier_service,
    run_cloud_web_service,
    run_cloud_worker_service,
)
from tests.unit.config.test_cloud_settings import _railway_settings
from tests.unit.experiments.test_runtime_policy import _live_manifest
from tests.unit.platform.test_registry_domain import _descriptor

NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
OPERATION_ID = UUID("22222222-2222-4222-8222-222222222222")
ATTEMPT_ID = UUID("33333333-3333-4333-8333-333333333333")
RECORD_ID = UUID("44444444-4444-4444-8444-444444444444")
ARTIFACT_ID = RECORD_ID


class Catalog:
    def __init__(self, record: ArtifactRecord) -> None:
        self.record = record
        self.requested: list[UUID] = []

    async def get_record(self, record_id: UUID) -> ArtifactRecord:
        self.requested.append(record_id)
        return self.record


class ExactVersionStore:
    def __init__(
        self,
        inventory: tuple[StoredArtifact, ...],
        objects: dict[str, bytes],
    ) -> None:
        self.inventory = {item.key: item for item in inventory}
        self.objects = objects
        self.head_requests: list[tuple[str, str | None]] = []
        self.read_requests: list[tuple[str, str | None]] = []

    async def head(self, key: str, *, version_id: str | None = None) -> StoredArtifact:
        self.head_requests.append((key, version_id))
        return self.inventory[key]

    async def read_chunks(self, key: str, *, version_id: str | None = None):
        self.read_requests.append((key, version_id))
        yield self.objects[key]


def test_cloud_runtime_commands_are_explicit_and_accept_no_arbitrary_manifest_path() -> None:
    parser = build_parser()

    assert parser.parse_args(["cloud-web"]).command == "cloud-web"
    assert parser.parse_args(["cloud-worker"]).command == "cloud-worker"
    assert parser.parse_args(["cloud-operations"]).command == "cloud-operations"
    verifier = parser.parse_args(["cloud-verifier", "--run-id", str(RUN_ID)])
    migration = parser.parse_args(
        ["cloud-migrate", "--expected-revision", "0022", "--repository", "."]
    )

    assert verifier.run_id == RUN_ID
    assert migration.expected_revision == "0022"
    for command in ("cloud-web", "cloud-worker", "cloud-operations"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--manifest", "mutable.json"])


@pytest.mark.parametrize(
    ("arguments", "target", "error_code"),
    (
        (("cloud-web",), "run_cloud_web_service", "cloud_web_unhandled_exception"),
        (("cloud-worker",), "run_cloud_worker_service", "cloud_worker_unhandled_exception"),
        (
            ("cloud-verifier", "--run-id", str(RUN_ID)),
            "run_cloud_verifier_service",
            "cloud_verifier_unhandled_exception",
        ),
    ),
)
def test_cloud_command_terminal_failure_is_never_reported_as_clean(
    arguments: tuple[str, ...],
    target: str,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*_args: object, **_values: object) -> None:
        raise RuntimeError("service failed")

    captured: list[str] = []
    monkeypatch.setattr("maais.cli.get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(f"maais.cli.{target}", fail)
    monkeypatch.setattr(
        "maais.cli._capture_exception_without_suppressing_exit",
        lambda _error, **values: captured.append(str(values["error_code"])),
    )

    assert main(arguments) == 1
    assert captured == [error_code]


def test_clean_cloud_command_flushes_sentry_before_reporting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Runtime:
        enabled = True
        initialization_error = None

        def __init__(self) -> None:
            self.flushes: list[float] = []

        def flush(self, *, timeout: float) -> bool:
            self.flushes.append(timeout)
            return True

    runtime = Runtime()

    async def stop_cleanly(*_args: object, **_values: object) -> None:
        return None

    monkeypatch.setattr("maais.cli.get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("maais.cli.initialize_backend_sentry", lambda _settings: runtime)
    monkeypatch.setattr("maais.cli.run_cloud_web_service", stop_cleanly)

    assert main(("cloud-web",)) == 0
    assert runtime.flushes == [5.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint", "required_role"),
    (
        ("web", ServiceRole.WEB),
        ("worker", ServiceRole.WORKER),
        ("operations", ServiceRole.OPERATIONS),
        ("verifier", ServiceRole.VERIFIER),
        ("migrator", ServiceRole.MIGRATOR),
    ),
)
async def test_role_entrypoints_reject_wrong_authority_before_creating_resources(
    entrypoint: str,
    required_role: ServiceRole,
) -> None:
    configured_role = next(role for role in ServiceRole if role is not required_role)
    database_role = {
        ServiceRole.WEB: "maais_web",
        ServiceRole.WORKER: "maais_worker",
        ServiceRole.OPERATIONS: "maais_ops",
        ServiceRole.VERIFIER: "maais_verifier",
        ServiceRole.MIGRATOR: "maais_migrator",
    }[configured_role]
    settings = _railway_settings(
        service_role=configured_role,
        database_role_name=database_role,
    )
    resources_created = False

    def backend_factory(_settings: Settings):
        nonlocal resources_created
        resources_created = True
        raise AssertionError("role mismatch must fail before database resources")

    @asynccontextmanager
    async def lifecycle_factory(**_values: object):
        nonlocal resources_created
        resources_created = True
        raise AssertionError("role mismatch must fail before lifecycle resources")
        yield SimpleNamespace()  # pragma: no cover

    with pytest.raises(ServiceRoleMismatch, match=required_role.value):
        if entrypoint == "web":
            await run_cloud_web_service(settings, backend_factory=backend_factory)
        elif entrypoint == "worker":
            await run_cloud_worker_service(settings, lifecycle_factory=lifecycle_factory)
        elif entrypoint == "operations":
            await run_cloud_operations_service(
                settings,
                sentry_runtime=SentryRuntime(enabled=False),
                backend_factory=backend_factory,
            )
        elif entrypoint == "verifier":
            await run_cloud_verifier_service(
                settings,
                run_id=RUN_ID,
                backend_factory=backend_factory,
            )
        else:
            await attest_cloud_migrator_service(
                settings,
                backend_factory=backend_factory,
            )

    assert resources_created is False


@pytest.mark.asyncio
async def test_verifier_rejects_nil_run_identity_before_creating_resources() -> None:
    resources_created = False

    def backend_factory(_settings: Settings):
        nonlocal resources_created
        resources_created = True
        raise AssertionError("nil run identity must fail before database resources")

    with pytest.raises(ValueError, match="non-nil"):
        await run_cloud_verifier_service(
            _railway_settings(
                service_role=ServiceRole.VERIFIER,
                database_role_name="maais_verifier",
            ),
            run_id=UUID(int=0),
            backend_factory=backend_factory,
        )

    assert resources_created is False


def test_cloud_web_uses_ipv6_port_and_distrusts_forwarded_headers() -> None:
    async def application(_scope: object, _receive: object, _send: object) -> None:
        return None

    server = build_cloud_web_server(application, port=12_345)

    assert server.config.host == "::"
    assert server.config.port == 12_345
    assert server.config.proxy_headers is False
    assert server.config.forwarded_allow_ips == ""
    assert server.config.workers == 1
    assert server.config.timeout_graceful_shutdown == 20
    assert server.config.access_log is False


@pytest.mark.asyncio
async def test_cloud_health_stays_unready_until_lifecycle_startup_checks_pass() -> None:
    class Reader:
        def __init__(self) -> None:
            self.readiness_calls = 0

        async def readiness(self) -> bool:
            self.readiness_calls += 1
            return True

        async def monitor(self):
            raise AssertionError("monitor must remain gated")

    lifecycle = SimpleNamespace(ready=False)
    delegate = Reader()
    reader = LifecycleCloudEndpointReader(lifecycle=lifecycle, delegate=delegate)

    assert await reader.readiness() is False
    snapshot = await reader.monitor()
    assert snapshot.ready is False
    assert all(value is False for value in snapshot.components.values())
    assert delegate.readiness_calls == 0

    lifecycle.ready = True
    assert await reader.readiness() is True
    assert delegate.readiness_calls == 1


@pytest.mark.asyncio
async def test_cloud_web_marks_ready_only_after_programmatic_server_startup() -> None:
    descriptor = _descriptor()
    stop_requested = asyncio.Event()
    evidence = RuntimeIdentityEvidence(
        identity=SimpleNamespace(
            boot_id=UUID("55555555-5555-4555-8555-555555555555"),
            candidate_hash=descriptor.descriptor_hash,
            environment_id="environment-1",
        ),
        schema_revision="0022",
        database_system_identifier_sha256="f" * 64,
    )

    class Lifecycle:
        def __init__(self) -> None:
            self.ready = False
            self.mark_ready_calls = 0
            self.removed_signals = False
            self.evidence = evidence
            self.stop_requested = stop_requested

        def mark_ready(self) -> None:
            self.mark_ready_calls += 1
            self.ready = True

        def request_stop(self) -> None:
            self.ready = False
            self.stop_requested.set()

        async def wait_until_stopped(self) -> None:
            await self.stop_requested.wait()

        def install_signal_handlers(self):
            def remove() -> None:
                self.removed_signals = True

            return remove

    lifecycle = Lifecycle()
    backend = SimpleNamespace(session_factory=object())

    @asynccontextmanager
    async def lifecycle_factory(**values: object):
        assert values["backend"] is backend
        yield lifecycle

    captured_app: dict[str, object] = {}

    def app_factory(**values: object) -> object:
        captured_app.update(values)

        async def stop_after_ready() -> None:
            while not lifecycle.ready:
                await asyncio.sleep(0)
            lifecycle.request_stop()

        asyncio.create_task(stop_after_ready())
        return object()

    class Server:
        def __init__(self) -> None:
            self.started = False
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

    server = Server()

    await run_cloud_web_service(
        _railway_settings(
            service_role="web",
            database_role_name="maais_web",
            railway_service_id="web-service",
            port=12_345,
        ),
        backend_factory=lambda _settings: backend,
        lifecycle_factory=lifecycle_factory,
        app_factory=app_factory,
        server_builder=lambda _application, *, port: server,
    )

    assert lifecycle.mark_ready_calls == 1
    assert lifecycle.removed_signals is True
    assert server.should_exit is True
    assert captured_app["session_factory"] is backend.session_factory
    assert isinstance(captured_app["cloud_health_reader"], LifecycleCloudEndpointReader)


@pytest.mark.asyncio
async def test_worker_manifest_retrieval_verifies_catalog_versions_bytes_and_candidate() -> None:
    descriptor, manifest, record, objects = _manifest_artifact()
    catalog = Catalog(record)
    store = ExactVersionStore(record.canonical_inventory, objects)

    restored = await load_verified_manifest_artifact(
        catalog=catalog,
        canonical_store=store,
        artifact_record_id=ARTIFACT_ID,
        expected_run_id=RUN_ID,
        expected_candidate=descriptor,
    )

    assert restored == manifest
    assert catalog.requested == [ARTIFACT_ID]
    assert set(store.head_requests) == {
        (item.key, item.version_id) for item in record.canonical_inventory
    }
    assert set(store.read_requests) == {
        (item.key, item.version_id) for item in record.canonical_inventory
    }


@pytest.mark.asyncio
async def test_cloud_worker_verifies_artifact_before_starting_in_standby() -> None:
    descriptor, manifest, _record, _objects = _manifest_artifact()
    events: list[str] = []
    stop_requested = asyncio.Event()
    lifecycle = SimpleNamespace(
        evidence=RuntimeIdentityEvidence(
            identity=SimpleNamespace(
                boot_id=UUID("55555555-5555-4555-8555-555555555555"),
                candidate_hash=descriptor.descriptor_hash,
            ),
            schema_revision="0022",
            database_system_identifier_sha256="f" * 64,
        ),
        stop_requested=stop_requested,
        ready=False,
        signal_handlers_removed=False,
    )

    def mark_ready() -> None:
        lifecycle.ready = True

    lifecycle.mark_ready = mark_ready

    def install_signal_handlers():
        events.append("signals_installed")

        def remove() -> None:
            lifecycle.signal_handlers_removed = True
            events.append("signals_removed")

        return remove

    lifecycle.install_signal_handlers = install_signal_handlers

    @asynccontextmanager
    async def lifecycle_factory(**values: object):
        assert values["run_id"] == RUN_ID
        events.append("identity_registered")
        yield lifecycle
        events.append("identity_stopped")

    async def manifest_loader(**values: object) -> ExperimentManifest:
        assert values["artifact_record_id"] == ARTIFACT_ID
        assert values["expected_run_id"] == RUN_ID
        assert values["expected_candidate"].descriptor_hash == descriptor.descriptor_hash
        events.append("manifest_verified")
        return manifest

    async def worker_runner(loaded: ExperimentManifest, **values: object) -> None:
        assert loaded == manifest
        assert values["worker_id"] == lifecycle.evidence.identity.boot_id
        assert values["platform_run_id"] == RUN_ID
        assert values["stop_event"] is stop_requested
        assert lifecycle.ready is False
        events.append("worker_created")
        values["status_writer"](
            {
                "event": "paper_live_started",
                "worker_state": "standby",
                "live_money": False,
            }
        )
        assert lifecycle.ready is True

    def candidate_loader(_settings: Settings) -> CandidateDescriptor:
        events.append("candidate_loaded")
        return descriptor

    settings = _railway_settings(
        cloud_run_id=RUN_ID,
        manifest_artifact_id=ARTIFACT_ID,
    )
    await run_cloud_worker_service(
        settings,
        lifecycle_factory=lifecycle_factory,
        manifest_loader=manifest_loader,
        worker_runner=worker_runner,
        candidate_loader=candidate_loader,
    )

    assert events == [
        "identity_registered",
        "candidate_loaded",
        "manifest_verified",
        "signals_installed",
        "worker_created",
        "signals_removed",
        "identity_stopped",
    ]
    assert lifecycle.signal_handlers_removed is True


@pytest.mark.asyncio
async def test_cloud_worker_refuses_missing_catalog_identity_before_any_runtime_work() -> None:
    called = False

    @asynccontextmanager
    async def lifecycle_factory(**_values: object):
        nonlocal called
        called = True
        yield SimpleNamespace()

    with pytest.raises(ValueError, match="MAAIS_MANIFEST_ARTIFACT_ID"):
        await run_cloud_worker_service(
            _railway_settings(cloud_run_id=RUN_ID),
            lifecycle_factory=lifecycle_factory,
        )

    assert called is False


@pytest.mark.asyncio
async def test_cloud_worker_rejects_embedded_candidate_different_from_registered_boot() -> None:
    descriptor = _descriptor()
    different = CandidateDescriptor.build(
        git_sha=descriptor.git_sha,
        source_clean=True,
        uv_lock_sha256=descriptor.uv_lock_sha256,
        dashboard_lock_sha256=descriptor.dashboard_lock_sha256,
        schema_revision=descriptor.schema_revision,
        agent_implementation_hashes=descriptor.agent_implementation_hashes,
        dashboard_asset_manifest_sha256=descriptor.dashboard_asset_manifest_sha256,
        build_definition_sha256="f" * 64,
    )
    lifecycle = SimpleNamespace(
        evidence=RuntimeIdentityEvidence(
            identity=SimpleNamespace(
                boot_id=UUID("55555555-5555-4555-8555-555555555555"),
                candidate_hash=descriptor.descriptor_hash,
            ),
            schema_revision="0022",
            database_system_identifier_sha256="e" * 64,
        ),
        stop_requested=asyncio.Event(),
    )
    manifest_loaded = False

    @asynccontextmanager
    async def lifecycle_factory(**_values: object):
        yield lifecycle

    async def manifest_loader(**_values: object) -> ExperimentManifest:
        nonlocal manifest_loaded
        manifest_loaded = True
        raise AssertionError("mismatched candidate must fail before artifact retrieval")

    with pytest.raises(ManifestArtifactIntegrityError, match="embedded_candidate"):
        await run_cloud_worker_service(
            _railway_settings(
                cloud_run_id=RUN_ID,
                manifest_artifact_id=ARTIFACT_ID,
            ),
            lifecycle_factory=lifecycle_factory,
            manifest_loader=manifest_loader,
            candidate_loader=lambda _settings: different,
        )

    assert manifest_loaded is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    (
        "catalog_hash",
        "bundle_hash",
        "candidate_hash",
        "object_bytes",
        "manifest_hash",
        "agent_hash",
    ),
)
async def test_worker_manifest_retrieval_fails_closed_on_every_identity_layer(
    corruption: str,
) -> None:
    descriptor, _manifest, record, objects = _manifest_artifact()
    expected_candidate = descriptor
    if corruption == "catalog_hash":
        object.__setattr__(record, "catalog_content_hash", "f" * 64)
    elif corruption == "bundle_hash":
        object.__setattr__(record, "bundle_content_hash", "f" * 64)
    elif corruption == "candidate_hash":
        expected_candidate = CandidateDescriptor.build(
            git_sha=descriptor.git_sha,
            source_clean=True,
            uv_lock_sha256=descriptor.uv_lock_sha256,
            dashboard_lock_sha256=descriptor.dashboard_lock_sha256,
            schema_revision=descriptor.schema_revision,
            agent_implementation_hashes=descriptor.agent_implementation_hashes,
            dashboard_asset_manifest_sha256=descriptor.dashboard_asset_manifest_sha256,
            build_definition_sha256="f" * 64,
        )
    elif corruption == "object_bytes":
        manifest_item = next(
            item
            for item in record.canonical_inventory
            if item.key.rsplit("/", 1)[-1] == "manifest.json"
        )
        objects[manifest_item.key] += b" "
    elif corruption in {"manifest_hash", "agent_hash"}:
        manifest_item = next(
            item
            for item in record.canonical_inventory
            if item.key.rsplit("/", 1)[-1] == "manifest.json"
        )
        envelope = json.loads(objects[manifest_item.key])
        if corruption == "manifest_hash":
            envelope["manifest_hash"] = "f" * 64
        else:
            envelope["manifest"]["agent_versions"][0]["implementation_hash"] = "f" * 64
        objects[manifest_item.key] = _canonical_json(envelope)

    with pytest.raises((ManifestArtifactIntegrityError, ValueError)):
        await load_verified_manifest_artifact(
            catalog=Catalog(record),
            canonical_store=ExactVersionStore(record.canonical_inventory, objects),
            artifact_record_id=ARTIFACT_ID,
            expected_run_id=RUN_ID,
            expected_candidate=expected_candidate,
        )


def _manifest_artifact() -> tuple[
    CandidateDescriptor,
    ExperimentManifest,
    ArtifactRecord,
    dict[str, bytes],
]:
    descriptor = _descriptor()
    base = _live_manifest()
    manifest = replace(
        base,
        git_sha=descriptor.git_sha,
        lock_hash=descriptor.uv_lock_sha256,
        schema_revision=descriptor.schema_revision,
        agent_versions=tuple(
            replace(
                entry,
                implementation_hash=descriptor.agent_implementation_hashes[entry.agent_name],
            )
            for entry in base.agent_versions
        ),
    )
    report_id = manifest.manifest_hash
    envelope = {
        "candidate_hash": descriptor.descriptor_hash,
        "manifest": manifest.to_dict(),
        "manifest_hash": manifest.manifest_hash,
        "report_id": report_id,
    }
    manifest_bytes = _canonical_json(envelope)
    bundle_manifest = {
        "artifacts": {
            "manifest.json": {
                "bytes": len(manifest_bytes),
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
        },
        "report_id": report_id,
    }
    bundle_bytes = _canonical_json(bundle_manifest)
    raw_by_relative_path = {
        "bundle-manifest.json": bundle_bytes,
        "manifest.json": manifest_bytes,
    }
    canonical = tuple(
        _stored(
            relative_path,
            raw,
            descriptor_hash=descriptor.descriptor_hash,
            experiment_id=manifest.experiment_id,
            report_id=report_id,
            store_name="worm_canonical",
            version_id=f"version-{index}",
        )
        for index, (relative_path, raw) in enumerate(
            sorted(raw_by_relative_path.items()),
            start=1,
        )
    )
    replica = tuple(
        replace(item, store_name="railway_replica", version_id=None) for item in canonical
    )
    bundle_hash = content_hash(
        [
            {
                "content_type": item.content_type,
                "relative_path": item.key.rsplit("/", 1)[-1],
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in canonical
        ]
    )
    record = ArtifactRecord.create(
        record_id=RECORD_ID,
        operation_id=OPERATION_ID,
        publication_attempt_id=ATTEMPT_ID,
        environment="qualification",
        candidate_hash=descriptor.descriptor_hash,
        experiment_id=manifest.experiment_id,
        run_id=RUN_ID,
        artifact_type=ArtifactType.MANIFEST,
        report_id=report_id,
        bundle_content_hash=bundle_hash,
        size_bytes=sum(item.size_bytes for item in canonical),
        media_type="application/octet-stream",
        generated_at=NOW,
        recorded_at=NOW + timedelta(seconds=2),
        producing_deployment_id="deployment-1",
        producing_service_id="operations-service",
        sequence=1,
        replica_inventory=replica,
        canonical_inventory=canonical,
        previous_evidence_hash=GENESIS_EVIDENCE_HASH,
    )
    objects = {item.key: raw_by_relative_path[item.key.rsplit("/", 1)[-1]] for item in canonical}
    return descriptor, manifest, record, objects


def _stored(
    relative_path: str,
    raw: bytes,
    *,
    descriptor_hash: str,
    experiment_id: UUID,
    report_id: str,
    store_name: str,
    version_id: str | None,
) -> StoredArtifact:
    return StoredArtifact(
        store_name=store_name,
        key=artifact_key(
            environment="qualification",
            candidate_hash=descriptor_hash,
            experiment_id=experiment_id,
            artifact_type=ArtifactType.MANIFEST.value,
            report_id=report_id,
            relative_path=relative_path,
        ),
        etag=f"etag-{relative_path}",
        version_id=version_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        content_type="application/json",
        retention=RetentionRequest(
            mode=RetentionMode.COMPLIANCE,
            retain_until=NOW + timedelta(days=365),
        ),
        stored_at=NOW + timedelta(seconds=1),
    )


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
