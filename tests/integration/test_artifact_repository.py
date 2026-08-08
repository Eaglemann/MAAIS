from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import BigInteger, CheckConstraint, func, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from maais.artifacts.models import (
    GENESIS_EVIDENCE_HASH,
    ArtifactPublicationAttempt,
    ArtifactRecord,
    ArtifactType,
    PublicationAttemptStatus,
    RetentionRequest,
    ScheduledOperation,
    ScheduledOperationStatus,
    ScheduledOperationType,
    StoredArtifact,
)
from maais.config.artifacts import RetentionMode
from maais.config.cloud import ServiceRole
from maais.db.connection import Base
from maais.db.models.artifacts import (
    ArtifactPublicationAttemptModel,
    ArtifactRecordModel,
    ScheduledOperationModel,
)
from maais.db.repositories.artifacts import ArtifactCatalogConflict
from maais.db.repositories.scheduled_operations import ScheduledOperationConflict
from maais.db.unit_of_work import UnitOfWork
from tests.integration.test_platform_repository import _descriptor, _prepare_run, _service

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
BERLIN_DATE = date(2026, 8, 8)
EXPERIMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
OWNER_ONE = UUID("33333333-3333-4333-8333-333333333333")
OWNER_TWO = UUID("44444444-4444-4444-8444-444444444444")
OPERATION_ID = UUID("55555555-5555-4555-8555-555555555555")
ATTEMPT_ONE = UUID("66666666-6666-4666-8666-666666666666")
ATTEMPT_TWO = UUID("77777777-7777-4777-8777-777777777777")
RECORD_ONE = UUID("88888888-8888-4888-8888-888888888888")
RECORD_TWO = UUID("99999999-9999-4999-8999-999999999999")

EXPECTED_COLUMNS = {
    "scheduled_operations": (
        "id",
        "run_id",
        "experiment_id",
        "operation_type",
        "berlin_date",
        "status",
        "owner_boot_id",
        "generated_at",
        "attempt",
        "result_artifact_ids",
        "reason_code",
        "started_at",
        "completed_at",
        "content_hash",
    ),
    "artifact_publication_attempts": (
        "id",
        "operation_id",
        "attempt",
        "bundle_content_hash",
        "status",
        "started_at",
        "completed_at",
        "reason_code",
        "content_hash",
    ),
    "artifact_records": (
        "id",
        "operation_id",
        "publication_attempt_id",
        "environment",
        "candidate_hash",
        "experiment_id",
        "run_id",
        "artifact_type",
        "report_id",
        "bundle_content_hash",
        "size_bytes",
        "media_type",
        "generated_at",
        "recorded_at",
        "producing_deployment_id",
        "producing_service_id",
        "sequence",
        "replica_inventory",
        "canonical_inventory",
        "previous_evidence_hash",
        "catalog_content_hash",
    ),
}


async def test_artifact_catalog_model_and_database_contract_are_exact(
    db_connection: AsyncConnection,
) -> None:
    models = (
        ScheduledOperationModel,
        ArtifactPublicationAttemptModel,
        ArtifactRecordModel,
    )
    assert {model.__table__.name for model in models} == set(EXPECTED_COLUMNS)
    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        assert tuple(column.name for column in table.columns) == expected_columns
        assert tuple(column.name for column in table.primary_key.columns) == ("id",)
        assert {
            str(constraint.name)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
    assert isinstance(ArtifactRecordModel.__table__.c.size_bytes.type, BigInteger)

    def compare(sync_connection: object) -> None:
        inspector = inspect(sync_connection)
        for table_name, expected_columns in EXPECTED_COLUMNS.items():
            assert (
                tuple(column["name"] for column in inspector.get_columns(table_name))
                == expected_columns
            )
            assert {item["name"] for item in inspector.get_check_constraints(table_name)} == {
                str(constraint.name)
                for constraint in Base.metadata.tables[table_name].constraints
                if isinstance(constraint, CheckConstraint)
            }
        assert isinstance(
            next(
                column["type"]
                for column in inspector.get_columns("artifact_records")
                if column["name"] == "size_bytes"
            ),
            BigInteger,
        )

    await db_connection.run_sync(compare)


async def test_scheduled_operation_is_exactly_once_and_active_owner_cannot_be_stolen(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    candidate = _operation(owner=OWNER_ONE, operation_id=OPERATION_ID, generated_at=NOW)
    async with uow_factory.begin() as uow:
        first = await uow.scheduled_operations.acquire(candidate)
        repeated = await uow.scheduled_operations.acquire(
            _operation(
                owner=OWNER_ONE,
                operation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                generated_at=NOW + timedelta(hours=1),
            )
        )
        with pytest.raises(ScheduledOperationConflict, match="active owner"):
            await uow.scheduled_operations.acquire(
                _operation(
                    owner=OWNER_TWO,
                    operation_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                    generated_at=NOW + timedelta(hours=1),
                )
            )

    assert first == repeated
    assert first.id == OPERATION_ID
    assert first.generated_at == NOW
    assert first.attempt == 1


async def test_takeover_requires_stopped_owner_and_preserves_identity_and_generated_at(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    async with uow_factory.begin() as uow:
        original = await uow.scheduled_operations.acquire(
            _operation(owner=OWNER_ONE, operation_id=OPERATION_ID, generated_at=NOW)
        )
        await uow.platform.stop_service_instance(
            boot_id=OWNER_ONE,
            reason="replacement",
            stopped_at=NOW + timedelta(minutes=1),
        )
    async with uow_factory.begin() as uow:
        taken_over = await uow.scheduled_operations.acquire(
            _operation(
                owner=OWNER_TWO,
                operation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                generated_at=NOW + timedelta(minutes=1),
                started_at=NOW + timedelta(minutes=2),
            )
        )

    assert taken_over.id == original.id
    assert taken_over.generated_at == original.generated_at
    assert taken_over.owner_boot_id == OWNER_TWO
    assert taken_over.attempt == 2


async def test_failed_operation_retries_monotonically_without_losing_failure(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    async with uow_factory.begin() as uow:
        operation = await uow.scheduled_operations.acquire(
            _operation(owner=OWNER_ONE, operation_id=OPERATION_ID, generated_at=NOW)
        )
        failed = await uow.scheduled_operations.fail(
            operation.id,
            owner_boot_id=OWNER_ONE,
            reason_code="replica_unavailable",
            failed_at=NOW + timedelta(minutes=1),
        )
    async with uow_factory.begin() as uow:
        retried = await uow.scheduled_operations.acquire(
            _operation(
                owner=OWNER_ONE,
                operation_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                generated_at=NOW + timedelta(minutes=1),
                started_at=NOW + timedelta(minutes=2),
            )
        )

    assert failed.status is ScheduledOperationStatus.FAILED
    assert retried.status is ScheduledOperationStatus.RUNNING
    assert retried.attempt == 2
    assert retried.generated_at == NOW


async def test_database_trigger_rejects_same_owner_restart_while_operation_is_running(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    operation = await _acquire_operation(uow_factory)

    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                update(ScheduledOperationModel)
                .where(ScheduledOperationModel.id == operation.id)
                .values(
                    attempt=operation.attempt + 1,
                    started_at=operation.started_at + timedelta(minutes=1),
                )
            )


async def test_publication_attempts_are_monotonic_and_failed_attempts_remain_append_only(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    operation = await _acquire_operation(uow_factory)
    first = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_ONE,
        operation_id=operation.id,
        attempt=1,
        bundle_content_hash="a" * 64,
        started_at=NOW + timedelta(minutes=1),
    )
    second = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_TWO,
        operation_id=operation.id,
        attempt=2,
        bundle_content_hash="a" * 64,
        started_at=NOW + timedelta(minutes=2),
    )
    async with uow_factory.begin() as uow:
        await uow.artifacts.start_attempt(first)
        failed = await uow.artifacts.fail_attempt(
            first.id,
            reason_code="canonical_timeout",
            failed_at=NOW + timedelta(minutes=1, seconds=30),
        )
        await uow.artifacts.start_attempt(second)
        count = await uow.session.scalar(
            select(func.count()).select_from(ArtifactPublicationAttemptModel)
        )

    assert failed.status is PublicationAttemptStatus.FAILED
    assert count == 2


async def test_database_trigger_rejects_successful_attempt_without_catalog_record(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    operation = await _acquire_operation(uow_factory)
    attempt = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_ONE,
        operation_id=operation.id,
        attempt=1,
        bundle_content_hash="a" * 64,
        started_at=NOW + timedelta(minutes=1),
    )
    completed_at = NOW + timedelta(minutes=2)
    succeeded = attempt.succeed(completed_at=completed_at)
    async with uow_factory.begin() as uow:
        await uow.artifacts.start_attempt(attempt)

    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                update(ArtifactPublicationAttemptModel)
                .where(ArtifactPublicationAttemptModel.id == attempt.id)
                .values(
                    status=PublicationAttemptStatus.SUCCEEDED.value,
                    completed_at=completed_at,
                    content_hash=succeeded.content_hash,
                )
            )


async def test_successful_record_is_idempotent_immutable_and_marks_attempt_succeeded(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    operation = await _acquire_operation(uow_factory)
    attempt = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_ONE,
        operation_id=operation.id,
        attempt=1,
        bundle_content_hash="a" * 64,
        started_at=NOW + timedelta(minutes=1),
    )
    record = _record(
        record_id=RECORD_ONE,
        attempt_id=attempt.id,
        sequence=1,
        previous_hash=GENESIS_EVIDENCE_HASH,
    )
    async with uow_factory.begin() as uow:
        await uow.artifacts.start_attempt(attempt)
        first = await uow.artifacts.record_publication(record)
        repeated = await uow.artifacts.record_publication(record)
        completed = await uow.scheduled_operations.complete(
            operation.id,
            owner_boot_id=OWNER_ONE,
            result_artifact_ids=(record.id,),
            completed_at=NOW + timedelta(minutes=4),
        )
        stored_attempt = await uow.session.get(ArtifactPublicationAttemptModel, attempt.id)

    changed = _record(
        record_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        attempt_id=attempt.id,
        sequence=1,
        previous_hash=GENESIS_EVIDENCE_HASH,
        canonical_version="different-version",
    )
    async with uow_factory.begin() as uow:
        with pytest.raises(ArtifactCatalogConflict, match="immutable"):
            await uow.artifacts.record_publication(changed)

    assert first == repeated == record
    assert completed.status is ScheduledOperationStatus.SUCCEEDED
    assert completed.result_artifact_ids == (record.id,)
    assert stored_attempt is not None
    assert stored_attempt.status == PublicationAttemptStatus.SUCCEEDED.value


async def test_catalog_chain_requires_exact_previous_hash_and_revalidates_on_read(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    operation = await _acquire_operation(uow_factory)
    first_attempt = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_ONE,
        operation_id=operation.id,
        attempt=1,
        bundle_content_hash="a" * 64,
        started_at=NOW + timedelta(minutes=1),
    )
    first = _record(
        record_id=RECORD_ONE,
        attempt_id=ATTEMPT_ONE,
        sequence=1,
        previous_hash=GENESIS_EVIDENCE_HASH,
    )
    async with uow_factory.begin() as uow:
        await uow.artifacts.start_attempt(first_attempt)
        await uow.artifacts.record_publication(first)

    second_attempt = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_TWO,
        operation_id=operation.id,
        attempt=2,
        bundle_content_hash="b" * 64,
        started_at=NOW + timedelta(minutes=2),
    )
    wrong = _record(
        record_id=RECORD_TWO,
        attempt_id=ATTEMPT_TWO,
        sequence=2,
        previous_hash="f" * 64,
        report_id="report-002",
        bundle_hash="b" * 64,
    )
    async with uow_factory.begin() as uow:
        await uow.artifacts.start_attempt(second_attempt)
        with pytest.raises(ArtifactCatalogConflict, match="previous evidence"):
            await uow.artifacts.record_publication(wrong)

    second = _record(
        record_id=RECORD_TWO,
        attempt_id=ATTEMPT_TWO,
        sequence=2,
        previous_hash=first.catalog_content_hash,
        report_id="report-002",
        bundle_hash="b" * 64,
    )
    async with uow_factory.begin() as uow:
        await uow.artifacts.record_publication(second)
        chain = await uow.artifacts.list_stream(
            environment="qualification",
            candidate_hash=_descriptor().descriptor_hash,
            experiment_id=EXPERIMENT_ID,
        )

    assert chain == (first, second)


async def test_database_trigger_rejects_successful_record_update_or_delete(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_authority(uow_factory)
    operation = await _acquire_operation(uow_factory)
    attempt = ArtifactPublicationAttempt.start(
        attempt_id=ATTEMPT_ONE,
        operation_id=operation.id,
        attempt=1,
        bundle_content_hash="a" * 64,
        started_at=NOW + timedelta(minutes=1),
    )
    record = _record(
        record_id=RECORD_ONE,
        attempt_id=ATTEMPT_ONE,
        sequence=1,
        previous_hash=GENESIS_EVIDENCE_HASH,
    )
    async with uow_factory.begin() as uow:
        await uow.artifacts.start_attempt(attempt)
        await uow.artifacts.record_publication(record)

    with pytest.raises(DBAPIError):
        async with uow_factory.begin() as uow:
            await uow.session.execute(
                update(ArtifactRecordModel)
                .where(ArtifactRecordModel.id == record.id)
                .values(canonical_inventory=[])
            )


async def _prepare_authority(uow_factory: UnitOfWork) -> None:
    await _prepare_run(uow_factory, experiment_id=EXPERIMENT_ID, run_id=RUN_ID)
    async with uow_factory.begin() as uow:
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ID,
                boot_id=OWNER_ONE,
                role=ServiceRole.OPERATIONS,
                service_id="operations-1",
            )
        )
        await uow.platform.register_service_instance(
            _service(
                run_id=RUN_ID,
                boot_id=OWNER_TWO,
                role=ServiceRole.OPERATIONS,
                service_id="operations-2",
            )
        )


def _operation(
    *,
    owner: UUID,
    operation_id: UUID,
    generated_at: datetime,
    started_at: datetime | None = None,
) -> ScheduledOperation:
    return ScheduledOperation.start(
        operation_id=operation_id,
        run_id=RUN_ID,
        experiment_id=EXPERIMENT_ID,
        operation_type=ScheduledOperationType.DAILY_REPORT,
        berlin_date=BERLIN_DATE,
        owner_boot_id=owner,
        generated_at=generated_at,
        started_at=started_at or max(generated_at, NOW) + timedelta(seconds=2),
    )


async def _acquire_operation(uow_factory: UnitOfWork) -> ScheduledOperation:
    async with uow_factory.begin() as uow:
        return await uow.scheduled_operations.acquire(
            _operation(owner=OWNER_ONE, operation_id=OPERATION_ID, generated_at=NOW)
        )


def _stored(*, canonical: bool, version_id: str | None = None) -> StoredArtifact:
    return StoredArtifact(
        store_name="canonical" if canonical else "replica",
        key=(
            "maais/qualification/"
            f"{_descriptor().descriptor_hash}/{EXPERIMENT_ID}/daily_report/report-001/report.json"
        ),
        etag='"multipart-etag-2"',
        version_id=version_id,
        sha256="a" * 64,
        size_bytes=128,
        content_type="application/json",
        retention=RetentionRequest(
            mode=RetentionMode.COMPLIANCE,
            retain_until=datetime(2026, 11, 6, tzinfo=timezone.utc),
        ),
        stored_at=NOW + timedelta(minutes=2),
    )


def _record(
    *,
    record_id: UUID,
    attempt_id: UUID,
    sequence: int,
    previous_hash: str,
    report_id: str = "report-001",
    bundle_hash: str = "a" * 64,
    canonical_version: str = "canonical-version-001",
) -> ArtifactRecord:
    return ArtifactRecord.create(
        record_id=record_id,
        operation_id=OPERATION_ID,
        publication_attempt_id=attempt_id,
        environment="qualification",
        candidate_hash=_descriptor().descriptor_hash,
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        artifact_type=ArtifactType.DAILY_REPORT,
        report_id=report_id,
        bundle_content_hash=bundle_hash,
        size_bytes=128,
        media_type="application/json",
        generated_at=NOW,
        recorded_at=NOW + timedelta(minutes=3, seconds=sequence),
        producing_deployment_id="deployment-1",
        producing_service_id="operations-1",
        sequence=sequence,
        replica_inventory=(_stored(canonical=False),),
        canonical_inventory=(_stored(canonical=True, version_id=canonical_version),),
        previous_evidence_hash=previous_hash,
    )
