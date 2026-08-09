"""Fail-closed component contract for unattended cloud operations health."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Protocol, cast
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.artifacts.models import ArtifactType
from maais.db.models.artifacts import ScheduledOperationModel
from maais.db.models.experiments import ExperimentModel
from maais.db.models.observability import HealthEvaluationModel
from maais.db.models.operations import (
    IncidentModel,
    MarketCursorModel,
    WorkerCheckpointModel,
    WorkerLeaseModel,
)
from maais.db.models.platform import (
    PlatformCandidateModel,
    RunInstanceModel,
    ServiceInstanceModel,
)
from maais.db.replay import verify_ledger_consistency
from maais.db.repositories.artifacts import ArtifactCatalogIntegrityError, ArtifactRepository
from maais.db.repositories.incidents import IncidentRepository
from maais.db.repositories.observability import ObservabilityRepository
from maais.db.unit_of_work import UnitOfWork
from maais.domain.json import JsonValue, freeze_json
from maais.observability.audit import (
    AuditSourceRole,
    HealthEvaluation,
    HealthSeverity,
    HealthStatus,
    deterministic_audit_event_id,
    health_deduplication_key,
    pseudonymous_reference,
)
from maais.operations.incidents import IncidentSeverity, IncidentState, IncidentStatus
from maais.platform.registry import RunStatus
from maais.platform.runtime import RuntimeIdentityEvidence

CRITICAL_COMPONENTS = frozenset(
    {
        "worker_continuity",
        "worker_lease",
        "database",
        "schema_identity",
        "cluster_identity",
        "ledger",
        "required_cursors",
        "dispatch_queue_capacity",
        "deployment_identity",
        "daily_close",
        "backup",
        "worm_replication",
        "audit_chain",
    }
)
WARNING_COMPONENTS = frozenset({"sentry_delivery"})
_COMPONENT_CONTRACT = CRITICAL_COMPONENTS | WARNING_COMPONENTS
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HEALTH_EVALUATION_NAMESPACE = UUID("0681a8e6-5d2c-4a20-8701-c2221f44112d")
_HEALTH_INCIDENT_NAMESPACE = UUID("89744a7f-2d77-4a36-9205-827dd9b9fc3a")
_HEALTH_LOCK_NAMESPACE = "maais:cloud-health:v1"
_SENTRY_DELIVERY_LOCK_NAMESPACE = "maais:sentry-cron-delivery:v1"
_SENTRY_CRON_OPERATIONS = frozenset({"daily_close", "backup", "evidence"})
_BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class CloudHealthComponent:
    passed: bool
    failure_severity: HealthSeverity
    reason_code: str
    evidence: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if type(self.passed) is not bool:
            raise TypeError("cloud health component passed must be a boolean")
        if self.failure_severity not in {HealthSeverity.WARNING, HealthSeverity.CRITICAL}:
            raise ValueError("cloud health component failure severity is invalid")
        if _REASON_CODE.fullmatch(self.reason_code) is None:
            raise ValueError("cloud health component reason code is invalid")
        normalized = freeze_json(self.evidence)
        if not isinstance(normalized, Mapping):
            raise TypeError("cloud health component evidence must be an object")
        object.__setattr__(self, "evidence", normalized)


@dataclass(frozen=True, slots=True)
class CloudHealthAssessment:
    overall_status: HealthStatus
    severity: HealthSeverity
    failed_check_names: tuple[str, ...]
    components: Mapping[str, Mapping[str, JsonValue]]
    checked_at: datetime


class CloudHealthSnapshotReader(Protocol):
    async def collect(
        self,
        run_id: UUID,
        checked_at: datetime,
    ) -> Mapping[str, CloudHealthComponent]: ...


class DatabaseCloudHealthSnapshotReader:
    """Derive the cloud health contract from one PostgreSQL read-only snapshot."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_evidence: RuntimeIdentityEvidence,
        environment: str,
        maximum_lag: timedelta = timedelta(minutes=3),
        dispatch_queue_capacity: int = 10_000,
        sentry_delivery_confirmed: Callable[[], bool] = lambda: True,
    ) -> None:
        if environment not in {"qualification", "production"}:
            raise ValueError("cloud health environment is invalid")
        if maximum_lag <= timedelta(0):
            raise ValueError("cloud health maximum lag must be positive")
        if dispatch_queue_capacity <= 0:
            raise ValueError("cloud health dispatch queue capacity must be positive")
        self._session_factory = session_factory
        self._runtime = runtime_evidence
        self._environment = environment
        self._maximum_lag = maximum_lag
        self._dispatch_queue_capacity = dispatch_queue_capacity
        self._sentry_delivery_confirmed = sentry_delivery_confirmed

    async def collect(
        self,
        run_id: UUID,
        checked_at: datetime,
    ) -> Mapping[str, CloudHealthComponent]:
        if checked_at.tzinfo is None or checked_at.utcoffset() != timedelta(0):
            raise ValueError("cloud health checked_at must be UTC-aware")
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                return await self._collect_snapshot(session, run_id, checked_at)

    async def _collect_snapshot(
        self,
        session: AsyncSession,
        run_id: UUID,
        checked_at: datetime,
    ) -> Mapping[str, CloudHealthComponent]:
        run = await session.get(RunInstanceModel, run_id)
        if run is None:
            raise LookupError("cloud health run does not exist")
        experiment = await session.get(ExperimentModel, run.experiment_id)
        candidate = await session.get(PlatformCandidateModel, run.candidate_hash)
        if experiment is None or candidate is None:
            raise LookupError("cloud health run authority is incomplete")
        worker = (
            await session.get(ServiceInstanceModel, run.activating_worker_boot_id)
            if run.activating_worker_boot_id is not None
            else None
        )
        operations = await session.get(
            ServiceInstanceModel,
            self._runtime.identity.boot_id,
        )
        checkpoint = await session.get(WorkerCheckpointModel, run.experiment_id)
        lease = await session.get(WorkerLeaseModel, run.experiment_id)
        cursors = tuple(
            await session.scalars(
                select(MarketCursorModel)
                .where(MarketCursorModel.experiment_id == run.experiment_id)
                .order_by(MarketCursorModel.symbol, MarketCursorModel.id)
            )
        )
        database = (
            (
                await session.execute(
                    text(
                        "SELECT transaction_timestamp() AS snapshot_at, "
                        "current_user AS current_user, "
                        "(SELECT version_num FROM public.alembic_version) AS schema_revision, "
                        "system_identifier::text AS system_identifier "
                        "FROM pg_catalog.pg_control_system()"
                    )
                )
            )
            .mappings()
            .one()
        )
        observability = ObservabilityRepository(session)
        audit = await observability.verify_audit_chain()
        ledger = await verify_ledger_consistency(session)
        sentry_incident_count = len(
            tuple(
                await session.scalars(
                    select(IncidentModel.id).where(
                        IncidentModel.experiment_id == run.experiment_id,
                        IncidentModel.component == "sentry_cron_delivery",
                        IncidentModel.status != IncidentStatus.RESOLVED.value,
                    )
                )
            )
        )

        prior_health = tuple(
            await session.scalars(
                select(HealthEvaluationModel)
                .where(HealthEvaluationModel.run_id == run_id)
                .order_by(HealthEvaluationModel.checked_at.desc())
                .limit(2)
            )
        )
        prior_queue_depths = tuple(
            depth
            for row in reversed(prior_health)
            if (depth := _stored_queue_depth(row.component_json)) is not None
        )

        artifact_error = False
        try:
            records = await ArtifactRepository(session, observability).list_stream(
                environment=self._environment,
                candidate_hash=run.candidate_hash,
                experiment_id=run.experiment_id,
            )
        except ArtifactCatalogIntegrityError:
            artifact_error = True
            records = ()
        required_date = checked_at.astimezone(_BERLIN).date() - timedelta(days=1)
        run_start = cast(datetime, run.started_at or run.created_at)
        evidence_due = required_date >= run_start.astimezone(_BERLIN).date()
        daily_operation = None
        if evidence_due:
            daily_operation = await session.scalar(
                select(ScheduledOperationModel)
                .where(
                    ScheduledOperationModel.run_id == run_id,
                    ScheduledOperationModel.operation_type == "daily_close",
                    ScheduledOperationModel.berlin_date == required_date,
                )
                .order_by(ScheduledOperationModel.attempt.desc())
                .limit(1)
            )
        daily_records = (
            tuple(record for record in records if record.operation_id == daily_operation.id)
            if daily_operation is not None
            else ()
        )
        report_records = tuple(
            record for record in daily_records if record.artifact_type is ArtifactType.DAILY_REPORT
        )
        backup_records = tuple(
            record
            for record in daily_records
            if record.artifact_type is ArtifactType.LOGICAL_BACKUP
        )

        snapshot_at = database["snapshot_at"]
        database_fresh = _within_snapshot_window(
            snapshot_at,
            checked_at,
            self._maximum_lag,
        )
        worker_fresh = worker is not None and _fresh(
            worker.last_heartbeat_at,
            checked_at,
            self._maximum_lag,
        )
        checkpoint_fresh = checkpoint is not None and _fresh(
            checkpoint.checkpoint_at,
            checked_at,
            self._maximum_lag,
        )
        worker_continuity = bool(
            run.status == RunStatus.ACTIVE.value
            and worker is not None
            and worker.boot_id == run.activating_worker_boot_id
            and worker.run_id == run_id
            and worker.service_role == "worker"
            and worker.candidate_hash == run.candidate_hash
            and worker.stopped_at is None
            and worker_fresh
            and checkpoint is not None
            and checkpoint.worker_id == worker.boot_id
            and checkpoint.status in {"running", "recovering"}
            and checkpoint_fresh
        )
        lease_healthy = bool(
            lease is not None
            and worker is not None
            and lease.worker_id == worker.boot_id
            and lease.status == "active"
            and lease.expires_at > checked_at
            and _fresh(lease.heartbeat_at, checked_at, self._maximum_lag)
        )
        expected_symbols_raw = experiment.manifest_json.get("symbols")
        expected_symbols = (
            {str(symbol) for symbol in expected_symbols_raw}
            if isinstance(expected_symbols_raw, list)
            else set()
        )
        cursor_symbols = {cursor.symbol for cursor in cursors}
        stale_cursors = sum(
            1
            for cursor in cursors
            if not _fresh(cursor.updated_at, checked_at, self._maximum_lag)
            or not _fresh(cursor.bar_close_at, checked_at, self._maximum_lag)
        )
        required_cursors = bool(
            expected_symbols
            and len(cursors) == len(expected_symbols)
            and cursor_symbols == expected_symbols
            and all(cursor.status == "active" for cursor in cursors)
            and stale_cursors == 0
        )
        queue_depth = _checkpoint_queue_depth(checkpoint)
        queue_growing = (
            queue_depth is not None
            and len(prior_queue_depths) == 2
            and prior_queue_depths[0] < prior_queue_depths[1] < queue_depth
        )
        queue_healthy = bool(
            queue_depth is not None
            and 0 <= queue_depth < self._dispatch_queue_capacity
            and not queue_growing
        )
        current_system_identifier = str(database["system_identifier"])
        current_system_hash = _sha256_ascii(current_system_identifier)
        schema_healthy = bool(
            str(database["schema_revision"]) == self._runtime.schema_revision
            and candidate.schema_revision == self._runtime.schema_revision
        )
        cluster_healthy = bool(
            current_system_identifier == run.database_system_identifier
            and current_system_hash == self._runtime.database_system_identifier_sha256
        )
        identity = self._runtime.identity
        deployment_healthy = bool(
            operations is not None
            and operations.run_id == run_id
            and operations.boot_id == identity.boot_id
            and operations.service_role == "operations"
            and operations.candidate_hash == identity.candidate_hash == run.candidate_hash
            and operations.project_id == identity.project_id
            and operations.environment_id == identity.environment_id
            and operations.service_id == identity.service_id
            and operations.deployment_id == identity.deployment_id
            and operations.replica_id == identity.replica_id
            and operations.region == identity.region
            and operations.stopped_at is None
            and str(database["current_user"]) == "maais_ops"
        )
        operation_succeeded = bool(
            daily_operation is not None and daily_operation.status == "succeeded"
        )
        daily_healthy = bool(
            not evidence_due
            or (not artifact_error and operation_succeeded and len(report_records) == 1)
        )
        backup_healthy = bool(
            not evidence_due
            or (not artifact_error and operation_succeeded and len(backup_records) == 1)
        )
        worm_healthy = bool(
            not evidence_due
            or (
                daily_healthy
                and backup_healthy
                and all(
                    record.replica_inventory and record.canonical_inventory
                    for record in (*report_records, *backup_records)
                )
            )
        )

        sentry_delivery = bool(self._sentry_delivery_confirmed() and sentry_incident_count == 0)
        critical = HealthSeverity.CRITICAL
        warning = HealthSeverity.WARNING
        components = {
            "database": _component(
                database_fresh,
                critical,
                "database_available",
                "database_snapshot_stale",
                {"snapshot_fresh": database_fresh},
            ),
            "schema_identity": _component(
                schema_healthy,
                critical,
                "schema_identity_verified",
                "schema_identity_mismatch",
                {
                    "candidate_revision": candidate.schema_revision,
                    "database_revision": str(database["schema_revision"]),
                    "expected_revision": self._runtime.schema_revision,
                },
            ),
            "cluster_identity": _component(
                cluster_healthy,
                critical,
                "cluster_identity_verified",
                "cluster_identity_mismatch",
                {
                    "actual_hash": current_system_hash,
                    "expected_hash": self._runtime.database_system_identifier_sha256,
                },
            ),
            "worker_continuity": _component(
                worker_continuity,
                critical,
                "worker_continuity_verified",
                "worker_continuity_failed",
                {
                    "checkpoint_fresh": checkpoint_fresh,
                    "service_fresh": worker_fresh,
                    "worker_status": checkpoint.status if checkpoint is not None else None,
                },
            ),
            "worker_lease": _component(
                lease_healthy,
                critical,
                "worker_lease_active",
                "worker_lease_invalid",
                {
                    "active": lease.status == "active" if lease is not None else False,
                    "epoch": lease.epoch if lease is not None else None,
                },
            ),
            "ledger": _component(
                ledger.ok,
                critical,
                "ledger_verified",
                "ledger_verification_failed",
                {"error_count": len(ledger.errors)},
            ),
            "required_cursors": _component(
                required_cursors,
                critical,
                "required_cursors_fresh",
                "required_cursors_invalid",
                {
                    "actual_count": len(cursors),
                    "expected_count": len(expected_symbols),
                    "halted_count": sum(1 for cursor in cursors if cursor.status == "halted"),
                    "stale_count": stale_cursors,
                },
            ),
            "dispatch_queue_capacity": _component(
                queue_healthy,
                critical,
                "dispatch_queue_healthy",
                ("dispatch_queue_growing" if queue_growing else "dispatch_queue_capacity_failed"),
                {
                    "capacity": self._dispatch_queue_capacity,
                    "depth": queue_depth,
                    "growing": queue_growing,
                },
            ),
            "deployment_identity": _component(
                deployment_healthy,
                critical,
                "deployment_identity_verified",
                "deployment_identity_mismatch",
                {"verified": deployment_healthy},
            ),
            "daily_close": _component(
                daily_healthy,
                critical,
                "daily_close_verified" if evidence_due else "daily_close_not_due",
                "daily_close_missing",
                {
                    "artifact_count": len(report_records),
                    "evidence_due": evidence_due,
                    "required_berlin_date": required_date.isoformat(),
                },
            ),
            "backup": _component(
                backup_healthy,
                critical,
                "backup_verified" if evidence_due else "backup_not_due",
                "backup_missing",
                {
                    "artifact_count": len(backup_records),
                    "evidence_due": evidence_due,
                    "required_berlin_date": required_date.isoformat(),
                },
            ),
            "worm_replication": _component(
                worm_healthy,
                critical,
                "worm_replication_verified" if evidence_due else "worm_replication_not_due",
                "worm_replication_invalid",
                {
                    "artifact_catalog_valid": not artifact_error,
                    "evidence_due": evidence_due,
                    "verified_artifact_count": len(report_records) + len(backup_records),
                },
            ),
            "audit_chain": _component(
                audit.ok,
                critical,
                "audit_chain_verified",
                "audit_chain_invalid",
                {"error_count": len(audit.errors), "event_count": audit.event_count},
            ),
            "sentry_delivery": _component(
                sentry_delivery,
                warning,
                "sentry_delivery_confirmed",
                "sentry_delivery_unconfirmed",
                {
                    "delivery_confirmed": sentry_delivery,
                    "unresolved_incident_count": sentry_incident_count,
                },
            ),
        }
        if set(components) != _COMPONENT_CONTRACT:  # pragma: no cover - source invariant
            raise RuntimeError("database cloud health component contract drifted")
        return MappingProxyType(components)


class CloudHealthEvaluator:
    """Persist one immutable health result without authority over trading state."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWork,
        snapshot_reader: CloudHealthSnapshotReader,
        service_boot_id: UUID,
    ) -> None:
        if service_boot_id.int == 0:
            raise ValueError("cloud health service boot identifier cannot be nil")
        self._uow_factory = uow_factory
        self._snapshot_reader = snapshot_reader
        self._service_boot_id = service_boot_id

    async def evaluate(self, run_id: UUID, checked_at: datetime) -> HealthEvaluation:
        if run_id.int == 0:
            raise ValueError("cloud health run identifier cannot be nil")
        components = await self._snapshot_reader.collect(run_id, checked_at)
        assessment = evaluate_cloud_components(components, checked_at=checked_at)
        evaluation_id = uuid5(
            _HEALTH_EVALUATION_NAMESPACE,
            f"{run_id}:{checked_at.isoformat()}",
        )
        deduplication_key = health_deduplication_key(
            run_id,
            assessment.failed_check_names,
        )

        async with self._uow_factory.begin() as uow:
            await uow.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 22006))"),
                {"key": f"{_HEALTH_LOCK_NAMESPACE}:{run_id}"},
            )
            run = await uow.platform.get_run(run_id)
            if run.status is not RunStatus.ACTIVE:
                raise ValueError("cloud health requires an active run")
            latest = await uow.observability.latest_health(run_id)
            if latest is not None and checked_at <= latest.checked_at:
                raise ValueError("cloud health evaluation time must advance")

            unresolved = tuple(
                incident
                for incident in await uow.incidents.get_unresolved(run.experiment_id)
                if incident.component == "cloud_health"
            )
            incident_id: UUID | None = None
            if assessment.overall_status is HealthStatus.HEALTHY:
                await _resolve_health_incidents(uow.incidents, unresolved, checked_at)
            else:
                matching = next(
                    (
                        incident
                        for incident in unresolved
                        if incident.evidence.get("health_deduplication_key") == deduplication_key
                    ),
                    None,
                )
                await _resolve_health_incidents(
                    uow.incidents,
                    tuple(incident for incident in unresolved if incident != matching),
                    checked_at,
                    resolution="health_condition_changed",
                )
                if matching is None:
                    matching = IncidentState.create(
                        incident_id=uuid5(
                            _HEALTH_INCIDENT_NAMESPACE,
                            f"{run_id}:{deduplication_key}:{evaluation_id}",
                        ),
                        experiment_id=run.experiment_id,
                        deduplication_key=(f"cloud_health:{deduplication_key}:{evaluation_id}"),
                        severity=(
                            IncidentSeverity.CRITICAL
                            if assessment.severity is HealthSeverity.CRITICAL
                            else IncidentSeverity.WARNING
                        ),
                        component="cloud_health",
                        reason_code="health_evaluation_failed",
                        evidence={
                            "failed_check_names": assessment.failed_check_names,
                            "health_deduplication_key": deduplication_key,
                        },
                        requires_operator_review=False,
                        detected_at=checked_at,
                    )
                    await uow.incidents.record(matching)
                incident_id = matching.incident_id

            recovery_of = (
                latest.evaluation_id
                if latest is not None
                and latest.overall_status is not HealthStatus.HEALTHY
                and assessment.overall_status is HealthStatus.HEALTHY
                else None
            )
            evaluation = HealthEvaluation.create(
                evaluation_id=evaluation_id,
                run_id=run_id,
                service_boot_id=self._service_boot_id,
                overall_status=assessment.overall_status,
                failed_check_names=assessment.failed_check_names,
                severity=assessment.severity,
                deduplication_key=deduplication_key,
                incident_id=incident_id,
                recovery_of_evaluation_id=recovery_of,
                recovered_at=checked_at if recovery_of is not None else None,
                components=assessment.components,
                checked_at=checked_at,
            )
            await uow.observability.record_health(evaluation)
            await uow.observability.append_audit(
                event_id=deterministic_audit_event_id(
                    "health.evaluated",
                    evaluation_id,
                ),
                source_role=AuditSourceRole.OPERATIONS,
                actor_reference=pseudonymous_reference(
                    "service",
                    self._service_boot_id,
                ),
                session_reference=None,
                event_code="health.evaluated",
                reason_code=f"health_{assessment.overall_status.value}",
                evidence={
                    "evaluation_id": str(evaluation_id),
                    "failed_check_names": assessment.failed_check_names,
                    "overall_status": assessment.overall_status.value,
                },
                run_id=run_id,
                service_boot_id=self._service_boot_id,
                occurred_at=checked_at,
            )
            return evaluation


async def reconcile_sentry_delivery_incident(
    uow_factory: UnitOfWork,
    *,
    experiment_id: UUID,
    operations: tuple[str, ...],
    delivery_confirmed: bool,
    observed_at: datetime,
    incident_id_factory: Callable[[], UUID] = uuid4,
) -> IncidentState | None:
    """Persist one deduplicated warning episode without controlling the operation result."""

    if experiment_id.int == 0:
        raise ValueError("Sentry delivery experiment identifier cannot be nil")
    if type(delivery_confirmed) is not bool:
        raise TypeError("Sentry delivery outcome must be a boolean")
    normalized_operations = tuple(sorted(set(operations)))
    if (
        not normalized_operations
        or len(normalized_operations) != len(operations)
        or not set(normalized_operations) <= _SENTRY_CRON_OPERATIONS
    ):
        raise ValueError("Sentry delivery operations are invalid")

    async with uow_factory.begin() as uow:
        await uow.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 22008))"),
            {"key": f"{_SENTRY_DELIVERY_LOCK_NAMESPACE}:{experiment_id}"},
        )
        unresolved = tuple(
            incident
            for incident in await uow.incidents.get_unresolved(experiment_id)
            if incident.component == "sentry_cron_delivery"
        )
        if delivery_confirmed:
            recovered: IncidentState | None = None
            for incident in unresolved:
                recovered = incident.resolve(
                    "sentry_cron_delivery",
                    "sentry_delivery_recovered",
                    observed_at,
                    operator_confirmed=False,
                )
                await uow.incidents.record(recovered)
            return recovered
        if unresolved:
            return unresolved[0]

        incident_id = incident_id_factory()
        incident = IncidentState.create(
            incident_id=incident_id,
            experiment_id=experiment_id,
            deduplication_key=(f"sentry_cron_delivery:{experiment_id}:{incident_id}"),
            severity=IncidentSeverity.WARNING,
            component="sentry_cron_delivery",
            reason_code="sentry_delivery_unconfirmed",
            evidence={"operations": normalized_operations},
            requires_operator_review=False,
            detected_at=observed_at,
        )
        await uow.incidents.record(incident)
        return incident


def evaluate_cloud_components(
    components: Mapping[str, CloudHealthComponent],
    *,
    checked_at: datetime,
) -> CloudHealthAssessment:
    if checked_at.tzinfo is None or checked_at.utcoffset() != timedelta(0):
        raise ValueError("cloud health checked_at must be UTC-aware")
    if set(components) != _COMPONENT_CONTRACT:
        raise ValueError("cloud health component contract is incomplete or contains extras")
    for name in CRITICAL_COMPONENTS:
        if components[name].failure_severity is not HealthSeverity.CRITICAL:
            raise ValueError(f"critical component {name} has invalid failure severity")
    for name in WARNING_COMPONENTS:
        if components[name].failure_severity is not HealthSeverity.WARNING:
            raise ValueError(f"warning component {name} has invalid failure severity")

    failed = tuple(sorted(name for name, component in components.items() if not component.passed))
    if any(name in CRITICAL_COMPONENTS for name in failed):
        status = HealthStatus.CRITICAL
        severity = HealthSeverity.CRITICAL
    elif failed:
        status = HealthStatus.WARNING
        severity = HealthSeverity.WARNING
    else:
        status = HealthStatus.HEALTHY
        severity = HealthSeverity.INFO
    normalized = {
        name: MappingProxyType(
            {
                "status": "ok" if component.passed else "failed",
                "reason_code": component.reason_code,
                "failure_severity": component.failure_severity.value,
                "evidence": component.evidence,
            }
        )
        for name, component in sorted(components.items())
    }
    return CloudHealthAssessment(
        overall_status=status,
        severity=severity,
        failed_check_names=failed,
        components=MappingProxyType(normalized),
        checked_at=checked_at,
    )


def _component(
    passed: bool,
    failure_severity: HealthSeverity,
    passed_reason: str,
    failed_reason: str,
    evidence: Mapping[str, object],
) -> CloudHealthComponent:
    normalized = freeze_json(evidence)
    if not isinstance(normalized, Mapping):  # pragma: no cover - Mapping input invariant
        raise TypeError("cloud health component evidence must be an object")
    return CloudHealthComponent(
        passed=passed,
        failure_severity=failure_severity,
        reason_code=passed_reason if passed else failed_reason,
        evidence=normalized,
    )


def _fresh(value: object, checked_at: datetime, maximum_lag: timedelta) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    age = checked_at - value.astimezone(UTC)
    return timedelta(0) <= age <= maximum_lag


def _within_snapshot_window(
    value: object,
    checked_at: datetime,
    maximum_lag: timedelta,
) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    return abs(checked_at - value.astimezone(UTC)) <= maximum_lag


def _checkpoint_queue_depth(checkpoint: WorkerCheckpointModel | None) -> int | None:
    if checkpoint is None:
        return None
    state = checkpoint.state_json.get("state")
    if not isinstance(state, Mapping):
        return None
    value = state.get("dispatch_queue_depth")
    return value if type(value) is int else None


def _stored_queue_depth(components: Mapping[str, object]) -> int | None:
    queue = components.get("dispatch_queue_capacity")
    if not isinstance(queue, Mapping):
        return None
    evidence = queue.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    depth = evidence.get("depth")
    return depth if type(depth) is int else None


def _sha256_ascii(value: str) -> str:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return ""
    return hashlib.sha256(encoded).hexdigest()


async def _resolve_health_incidents(
    repository: IncidentRepository,
    incidents: tuple[IncidentState, ...],
    resolved_at: datetime,
    *,
    resolution: str = "health_recovered",
) -> None:
    for incident in incidents:
        if incident.status is IncidentStatus.RESOLVED:  # pragma: no cover - unresolved query
            continue
        await repository.record(
            incident.resolve(
                "cloud_health",
                resolution,
                resolved_at,
                operator_confirmed=False,
            )
        )
