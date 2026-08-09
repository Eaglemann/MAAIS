"""Authenticated, hash-verified read models for cloud operations evidence."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import and_, distinct, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from maais.api.schemas import (
    CloudArtifactPage,
    CloudArtifactView,
    CloudAuditEventPage,
    CloudAuditEventView,
    CloudCandidateView,
    CloudHealthEvaluationPage,
    CloudHealthEvaluationView,
    CloudIncidentPage,
    CloudIncidentView,
    CloudRunView,
    CloudServicePage,
    CloudServiceView,
    CloudStoredArtifactView,
)
from maais.artifacts.models import ArtifactRecord, StoredArtifact
from maais.db.models.artifacts import ArtifactRecordModel
from maais.db.models.observability import AuditEventModel, HealthEvaluationModel
from maais.db.models.operations import IncidentModel
from maais.db.models.platform import PlatformCandidateModel, RunInstanceModel
from maais.db.repositories.artifacts import ArtifactRepository
from maais.db.repositories.events import EventRepository
from maais.db.repositories.incidents import IncidentRepository
from maais.db.repositories.observability import ObservabilityRepository
from maais.db.repositories.platform import PlatformRepository
from maais.domain.json import content_hash, to_json_data
from maais.observability.audit import AuditEvent, HealthEvaluation
from maais.operations.incidents import IncidentState
from maais.platform.identity import CandidateDescriptor
from maais.platform.registry import CandidateStatus, PlatformCandidate, PlatformRun, ServiceInstance

MAX_CLOUD_PAGE_SIZE = 100
_RUN_INCIDENT_LIMIT = 25
_URL_USERINFO = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_BEARER_VALUE = re.compile(r"^bearer\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_VALUE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_DATABASE_SCHEMES = ("postgresql://", "postgres://", "mysql://", "redis://")
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "csrf",
        "csrf_token",
        "database_dsn",
        "database_url",
        "dsn",
        "ip",
        "ip_address",
        "monitor_token",
        "password",
        "password_hash",
        "private_key",
        "provider_secret",
        "raw_exception",
        "secret",
        "session_cookie",
        "token",
        "user_agent",
    }
)


class CloudEvidenceIntegrityError(RuntimeError):
    """Stored cloud evidence is inconsistent or unsafe to disclose."""


class CloudOperationsQueryService:
    """Serve cloud projections from one caller-owned read-only snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._observability = ObservabilityRepository(session)
        self._platform = PlatformRepository(session, self._observability)
        self._artifacts = ArtifactRepository(session, self._observability)
        self._incidents = IncidentRepository(session, EventRepository(session))

    async def get_candidate(self, candidate_hash: str) -> CloudCandidateView:
        row = await self._session.get(PlatformCandidateModel, candidate_hash)
        if row is None:
            raise LookupError("platform candidate does not exist")
        with _verified_evidence():
            descriptor = CandidateDescriptor.from_json_data(row.descriptor_json)
            if (
                descriptor.descriptor_hash != row.descriptor_hash
                or descriptor.git_sha != row.git_sha
                or descriptor.schema_revision != row.schema_revision
            ):
                raise CloudEvidenceIntegrityError("candidate columns do not match descriptor")
            candidate = PlatformCandidate(
                descriptor=descriptor,
                status=CandidateStatus(row.status),
                creator_deployment_id=row.creator_deployment_id,
                registered_at=row.registered_at,
                qualifying_at=row.qualifying_at,
                qualified_at=row.qualified_at,
                qualification_evidence_hash=row.qualification_evidence_hash,
            )
            return _candidate_view(candidate)

    async def get_run(self, run_id: UUID) -> CloudRunView:
        with _verified_evidence():
            run = await self._platform.get_run(run_id)
            incidents = await self._run_incidents(
                run,
                before_at=None,
                before_id=None,
                limit=_RUN_INCIDENT_LIMIT,
            )
            return _run_view(run, incidents.items)

    async def find_experiment_run(self, experiment_id: UUID) -> CloudRunView | None:
        run_id = await self._session.scalar(
            select(RunInstanceModel.id).where(RunInstanceModel.experiment_id == experiment_id)
        )
        if run_id is None:
            return None
        return await self.get_run(run_id)

    async def list_services(
        self,
        run_id: UUID,
        *,
        before_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> CloudServicePage:
        _validate_datetime_cursor(before_at, before_id)
        _validate_limit(limit)
        with _verified_evidence():
            run = await self._platform.get_run(run_id)
            services = sorted(
                await self._platform.list_run_services(run_id),
                key=lambda item: (item.first_seen_at, item.boot_id.int),
                reverse=True,
            )
            if any(
                service.run_id != run.id
                or service.identity.candidate_hash != run.candidate_hash
                or service.identity.environment_id != run.railway_environment_id
                for service in services
            ):
                raise CloudEvidenceIntegrityError("service is not owned by the requested run")
            if before_at is not None and before_id is not None:
                services = [
                    service
                    for service in services
                    if (service.first_seen_at, service.boot_id.int) < (before_at, before_id.int)
                ]
            selected = services[: limit + 1]
            has_more = len(selected) > limit
            visible = selected[:limit]
            tail = visible[-1] if has_more and visible else None
            return CloudServicePage(
                items=tuple(_service_view(service) for service in visible),
                limit=limit,
                has_more=has_more,
                next_before_at=tail.first_seen_at if tail is not None else None,
                next_before_id=tail.boot_id if tail is not None else None,
            )

    async def list_health(
        self,
        run_id: UUID,
        *,
        before_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> CloudHealthEvaluationPage:
        _validate_datetime_cursor(before_at, before_id)
        _validate_limit(limit)
        with _verified_evidence():
            await self._platform.get_run(run_id)
            statement = select(HealthEvaluationModel.evaluation_id).where(
                HealthEvaluationModel.run_id == run_id
            )
            if before_at is not None and before_id is not None:
                statement = statement.where(
                    or_(
                        HealthEvaluationModel.checked_at < before_at,
                        and_(
                            HealthEvaluationModel.checked_at == before_at,
                            HealthEvaluationModel.evaluation_id < before_id,
                        ),
                    )
                )
            identifiers = tuple(
                await self._session.scalars(
                    statement.order_by(
                        HealthEvaluationModel.checked_at.desc(),
                        HealthEvaluationModel.evaluation_id.desc(),
                    ).limit(limit + 1)
                )
            )
            has_more = len(identifiers) > limit
            evaluations = tuple(
                [
                    await self._observability.get_health(identifier)
                    for identifier in identifiers[:limit]
                ]
            )
            if any(evaluation.run_id != run_id for evaluation in evaluations):
                raise CloudEvidenceIntegrityError("health evidence is not scoped to the run")
            for evaluation in evaluations:
                _assert_disclosable_json(evaluation.components)
            tail = evaluations[-1] if has_more and evaluations else None
            return CloudHealthEvaluationPage(
                items=tuple(_health_view(evaluation) for evaluation in evaluations),
                limit=limit,
                has_more=has_more,
                next_before_at=tail.checked_at if tail is not None else None,
                next_before_id=tail.evaluation_id if tail is not None else None,
            )

    async def list_incidents(
        self,
        run_id: UUID,
        *,
        before_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> CloudIncidentPage:
        _validate_datetime_cursor(before_at, before_id)
        _validate_limit(limit)
        with _verified_evidence():
            run = await self._platform.get_run(run_id)
            return await self._run_incidents(
                run,
                before_at=before_at,
                before_id=before_id,
                limit=limit,
            )

    async def list_artifacts(
        self,
        run_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> CloudArtifactPage:
        _validate_limit(limit)
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("artifact cursor must be positive")
        with _verified_evidence():
            run = await self._platform.get_run(run_id)
            environments = tuple(
                await self._session.scalars(
                    select(distinct(ArtifactRecordModel.environment)).where(
                        ArtifactRecordModel.run_id == run_id
                    )
                )
            )
            if len(environments) > 1:
                raise CloudEvidenceIntegrityError(
                    "one run cannot mix qualification and production artifact streams"
                )
            records: tuple[ArtifactRecord, ...] = ()
            if environments:
                records = await self._artifacts.list_stream(
                    environment=environments[0],
                    candidate_hash=run.candidate_hash,
                    experiment_id=run.experiment_id,
                )
            if any(record.run_id != run_id for record in records):
                raise CloudEvidenceIntegrityError("artifact stream is not owned by the run")
            ordered = sorted(records, key=lambda record: record.sequence, reverse=True)
            if before_sequence is not None:
                ordered = [record for record in ordered if record.sequence < before_sequence]
            selected = ordered[: limit + 1]
            has_more = len(selected) > limit
            visible = selected[:limit]
            tail = visible[-1] if has_more and visible else None
            return CloudArtifactPage(
                items=tuple(_artifact_view(record) for record in visible),
                limit=limit,
                has_more=has_more,
                next_before_sequence=tail.sequence if tail is not None else None,
            )

    async def list_audit_events(
        self,
        run_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> CloudAuditEventPage:
        _validate_limit(limit)
        if before_sequence is not None and before_sequence < 1:
            raise ValueError("audit cursor must be positive")
        with _verified_evidence():
            await self._platform.get_run(run_id)
            verification = await self._observability.verify_audit_chain()
            if not verification.ok:
                raise CloudEvidenceIntegrityError("audit chain verification failed")
            statement = select(AuditEventModel.sequence).where(AuditEventModel.run_id == run_id)
            if before_sequence is not None:
                statement = statement.where(AuditEventModel.sequence < before_sequence)
            sequences = tuple(
                await self._session.scalars(
                    statement.order_by(AuditEventModel.sequence.desc()).limit(limit + 1)
                )
            )
            has_more = len(sequences) > limit
            events = tuple(
                [await self._observability.get_audit(sequence) for sequence in sequences[:limit]]
            )
            if any(event.run_id != run_id for event in events):
                raise CloudEvidenceIntegrityError("audit evidence is not scoped to the run")
            for event in events:
                _assert_disclosable_json(event.evidence)
            tail = events[-1] if has_more and events else None
            return CloudAuditEventPage(
                items=tuple(_audit_view(event) for event in events),
                limit=limit,
                has_more=has_more,
                next_before_sequence=tail.sequence if tail is not None else None,
            )

    async def _run_incidents(
        self,
        run: PlatformRun,
        *,
        before_at: datetime | None,
        before_id: UUID | None,
        limit: int,
    ) -> CloudIncidentPage:
        statement = select(IncidentModel).where(IncidentModel.experiment_id == run.experiment_id)
        if before_at is not None and before_id is not None:
            statement = statement.where(
                or_(
                    IncidentModel.detected_at < before_at,
                    and_(
                        IncidentModel.detected_at == before_at,
                        IncidentModel.id < before_id,
                    ),
                )
            )
        rows = tuple(
            await self._session.scalars(
                statement.order_by(
                    IncidentModel.detected_at.desc(),
                    IncidentModel.id.desc(),
                ).limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        incidents: list[CloudIncidentView] = []
        for row in visible_rows:
            if content_hash(row.state_json) != row.content_hash:
                raise CloudEvidenceIntegrityError("incident content hash verification failed")
            incident = await self._incidents.get(row.id)
            if incident.experiment_id != run.experiment_id:
                raise CloudEvidenceIntegrityError("incident is not scoped to the run")
            _assert_disclosable_json(incident.evidence)
            incidents.append(_incident_view(incident, content_hash_value=row.content_hash))
        tail = visible_rows[-1] if has_more and visible_rows else None
        return CloudIncidentPage(
            items=tuple(incidents),
            limit=limit,
            has_more=has_more,
            next_before_at=tail.detected_at if tail is not None else None,
            next_before_id=tail.id if tail is not None else None,
        )


@contextmanager
def _verified_evidence() -> Iterator[None]:
    try:
        yield
    except (CloudEvidenceIntegrityError, LookupError):
        raise
    except Exception as error:
        raise CloudEvidenceIntegrityError("cloud evidence validation failed") from error


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_CLOUD_PAGE_SIZE:
        raise ValueError(f"cloud evidence page limit must be 1-{MAX_CLOUD_PAGE_SIZE}")


def _validate_datetime_cursor(before_at: datetime | None, before_id: UUID | None) -> None:
    if (before_at is None) != (before_id is None):
        raise ValueError("cloud evidence cursor requires both timestamp and identifier")


def _assert_disclosable_json(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).casefold().replace("-", "_")
            if key in _FORBIDDEN_METADATA_KEYS or key.endswith(
                ("_cookie", "_dsn", "_password", "_secret", "_token")
            ):
                raise CloudEvidenceIntegrityError("cloud evidence contains forbidden metadata")
            _assert_disclosable_json(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_disclosable_json(nested)
        return
    if not isinstance(value, str):
        return
    lowered = value.casefold()
    if (
        lowered.startswith(_DATABASE_SCHEMES)
        or _BEARER_VALUE.match(value)
        or _PRIVATE_KEY_VALUE.search(value)
    ):
        raise CloudEvidenceIntegrityError("cloud evidence contains forbidden metadata")
    if _URL_USERINFO.match(value):
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise CloudEvidenceIntegrityError("cloud evidence contains forbidden metadata")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return
    raise CloudEvidenceIntegrityError("cloud evidence contains forbidden metadata")


def _candidate_view(candidate: PlatformCandidate) -> CloudCandidateView:
    descriptor = candidate.descriptor
    return CloudCandidateView(
        descriptor_hash=descriptor.descriptor_hash,
        git_sha=descriptor.git_sha,
        source_clean=descriptor.source_clean,
        uv_lock_sha256=descriptor.uv_lock_sha256,
        dashboard_lock_sha256=descriptor.dashboard_lock_sha256,
        schema_revision=descriptor.schema_revision,
        agent_implementation_hashes=descriptor.agent_implementation_hashes,
        dashboard_asset_manifest_sha256=descriptor.dashboard_asset_manifest_sha256,
        build_definition_sha256=descriptor.build_definition_sha256,
        status=candidate.status.value,
        creator_deployment_id=candidate.creator_deployment_id,
        registered_at=candidate.registered_at,
        qualifying_at=candidate.qualifying_at,
        qualified_at=candidate.qualified_at,
        qualification_evidence_hash=candidate.qualification_evidence_hash,
    )


def _run_view(run: PlatformRun, incidents: tuple[CloudIncidentView, ...]) -> CloudRunView:
    return CloudRunView(
        id=run.id,
        experiment_id=run.experiment_id,
        candidate_hash=run.candidate_hash,
        manifest_hash=run.manifest_hash,
        database_system_identifier=run.database_system_identifier,
        railway_environment_id=run.railway_environment_id,
        purpose=run.purpose.value,
        status=run.status.value,
        requested_operator_command_id=run.requested_operator_command_id,
        activating_worker_boot_id=run.activating_worker_boot_id,
        continuity_invalidated=run.continuity_invalidated,
        started_at=run.started_at,
        invalidated_at=run.invalidated_at,
        invalidation_reason=run.invalidation_reason,
        created_at=run.created_at,
        incidents=incidents,
    )


def _service_view(service: ServiceInstance) -> CloudServiceView:
    if service.run_id is None:
        raise CloudEvidenceIntegrityError("run service is missing its run identity")
    identity = service.identity
    return CloudServiceView(
        boot_id=service.boot_id,
        run_id=service.run_id,
        project_id=identity.project_id,
        environment_id=identity.environment_id,
        service_id=identity.service_id,
        deployment_id=identity.deployment_id,
        snapshot_id=identity.snapshot_id,
        replica_id=identity.replica_id,
        region=identity.region,
        service_role=identity.service_role.value,
        candidate_hash=identity.candidate_hash,
        started_at=identity.started_at,
        first_seen_at=service.first_seen_at,
        last_heartbeat_at=service.last_heartbeat_at,
        heartbeat_sequence=service.heartbeat_sequence,
        stopped_at=service.stopped_at,
        terminal_reason=service.terminal_reason,
    )


def _health_view(evaluation: HealthEvaluation) -> CloudHealthEvaluationView:
    return CloudHealthEvaluationView(
        evaluation_id=evaluation.evaluation_id,
        run_id=evaluation.run_id,
        service_boot_id=evaluation.service_boot_id,
        overall_status=evaluation.overall_status.value,
        failed_check_names=evaluation.failed_check_names,
        severity=evaluation.severity.value,
        deduplication_key=evaluation.deduplication_key,
        incident_id=evaluation.incident_id,
        recovery_of_evaluation_id=evaluation.recovery_of_evaluation_id,
        recovered_at=evaluation.recovered_at,
        components=_json_object(evaluation.components),
        checked_at=evaluation.checked_at,
        content_hash=evaluation.content_hash,
    )


def _incident_view(
    incident: IncidentState,
    *,
    content_hash_value: str,
) -> CloudIncidentView:
    return CloudIncidentView(
        id=incident.incident_id,
        experiment_id=incident.experiment_id,
        deduplication_key=incident.deduplication_key,
        severity=incident.severity.value,
        component=incident.component,
        reason_code=incident.reason_code,
        evidence=_json_object(incident.evidence),
        requires_operator_review=incident.requires_operator_review,
        status=incident.status.value,
        detected_at=incident.detected_at,
        acknowledged_at=incident.acknowledged_at,
        resolved_at=incident.resolved_at,
        acknowledged_by=incident.acknowledged_by,
        resolved_by=incident.resolved_by,
        resolution=incident.resolution,
        changed_at=incident.changed_at,
        version=incident.version,
        content_hash=content_hash_value,
    )


def _stored_artifact_view(item: StoredArtifact) -> CloudStoredArtifactView:
    return CloudStoredArtifactView(
        store_name=item.store_name,
        key=item.key,
        etag=item.etag,
        version_id=item.version_id,
        sha256=item.sha256,
        size_bytes=item.size_bytes,
        content_type=item.content_type,
        retention_mode=item.retention.mode.value,
        retain_until=item.retention.retain_until,
        stored_at=item.stored_at,
    )


def _artifact_view(record: ArtifactRecord) -> CloudArtifactView:
    return CloudArtifactView(
        id=record.id,
        operation_id=record.operation_id,
        publication_attempt_id=record.publication_attempt_id,
        environment=record.environment,
        candidate_hash=record.candidate_hash,
        experiment_id=record.experiment_id,
        run_id=record.run_id,
        artifact_type=record.artifact_type.value,
        report_id=record.report_id,
        bundle_content_hash=record.bundle_content_hash,
        size_bytes=record.size_bytes,
        media_type=record.media_type,
        generated_at=record.generated_at,
        recorded_at=record.recorded_at,
        producing_deployment_id=record.producing_deployment_id,
        producing_service_id=record.producing_service_id,
        sequence=record.sequence,
        replica_inventory=tuple(_stored_artifact_view(item) for item in record.replica_inventory),
        canonical_inventory=tuple(
            _stored_artifact_view(item) for item in record.canonical_inventory
        ),
        previous_evidence_hash=record.previous_evidence_hash,
        catalog_content_hash=record.catalog_content_hash,
    )


def _audit_view(event: AuditEvent) -> CloudAuditEventView:
    if event.run_id is None:
        raise CloudEvidenceIntegrityError("run audit event is missing its run identity")
    return CloudAuditEventView(
        event_id=event.event_id,
        sequence=event.sequence,
        previous_hash=event.previous_hash,
        source_role=event.source_role.value,
        actor_reference=event.actor_reference,
        session_reference=event.session_reference,
        event_code=event.event_code,
        reason_code=event.reason_code,
        evidence=_json_object(event.evidence),
        run_id=event.run_id,
        service_boot_id=event.service_boot_id,
        occurred_at=event.occurred_at,
        content_hash=event.content_hash,
    )


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping input invariant
        raise CloudEvidenceIntegrityError("cloud evidence JSON must be an object")
    return cast(dict[str, object], normalized)
