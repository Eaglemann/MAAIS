from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.config.cloud import ServiceRole
from maais.config.constants import ALL_AGENTS
from maais.platform.identity import CandidateDescriptor, RailwayRuntimeIdentity
from maais.platform.registry import (
    CandidateStatus,
    CandidateTransitionError,
    PlatformCandidate,
    PlatformRun,
    RunPurpose,
    RunStatus,
    RunTransitionError,
    ServiceInstance,
    ServiceInstanceConflict,
)

NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


def test_candidate_qualification_is_monotonic_and_terminal() -> None:
    registered = PlatformCandidate.register(
        _descriptor(),
        creator_deployment_id="deployment-1",
        registered_at=NOW,
    )
    qualifying = registered.begin_qualification(NOW + timedelta(seconds=1))
    qualified = qualifying.qualify("f" * 64, NOW + timedelta(seconds=2))

    assert registered.status is CandidateStatus.REGISTERED
    assert qualifying.status is CandidateStatus.QUALIFYING
    assert qualified.status is CandidateStatus.QUALIFIED
    assert qualified.qualification_evidence_hash == "f" * 64
    with pytest.raises(CandidateTransitionError, match="terminal"):
        qualified.begin_qualification(NOW + timedelta(seconds=3))
    with pytest.raises(CandidateTransitionError, match="qualifying"):
        registered.qualify("e" * 64, NOW + timedelta(seconds=1))


def test_candidate_rejection_freezes_evidence_and_rejects_time_regression() -> None:
    registered = PlatformCandidate.register(
        _descriptor(),
        creator_deployment_id="deployment-1",
        registered_at=NOW,
    )
    qualifying = registered.begin_qualification(NOW + timedelta(seconds=1))
    rejected = qualifying.reject("e" * 64, NOW + timedelta(seconds=2))

    assert rejected.status is CandidateStatus.REJECTED
    with pytest.raises(CandidateTransitionError, match="terminal"):
        rejected.qualify("f" * 64, NOW + timedelta(seconds=3))
    with pytest.raises(ValueError, match="cannot precede"):
        qualifying.reject("e" * 64, NOW)


def test_run_requires_explicit_command_and_same_worker_boot_then_stays_invalidated() -> None:
    standby = _run()
    with pytest.raises(RunTransitionError, match="operator command"):
        standby.activate(
            command_id=None,
            worker_boot_id=UUID(int=22),
            started_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(RunTransitionError, match="worker boot"):
        standby.activate(
            command_id=UUID(int=21),
            worker_boot_id=None,
            started_at=NOW + timedelta(seconds=1),
        )

    active = standby.activate(
        command_id=UUID(int=21),
        worker_boot_id=UUID(int=22),
        started_at=NOW + timedelta(seconds=1),
    )
    invalidated = active.invalidate(
        "unexpected_worker_boot",
        NOW + timedelta(seconds=2),
    )

    assert active.status is RunStatus.ACTIVE
    assert invalidated.status is RunStatus.INVALIDATED
    assert invalidated.continuity_invalidated is True
    with pytest.raises(RunTransitionError, match="invalidated"):
        invalidated.activate(
            command_id=UUID(int=21),
            worker_boot_id=UUID(int=22),
            started_at=NOW + timedelta(seconds=3),
        )
    with pytest.raises(RunTransitionError, match="invalidated"):
        invalidated.complete()


def test_run_completion_is_terminal_and_standby_can_be_invalidated() -> None:
    active = _run().activate(
        command_id=UUID(int=21),
        worker_boot_id=UUID(int=22),
        started_at=NOW + timedelta(seconds=1),
    )
    completed = active.complete()
    prestart_invalid = _run().invalidate("operator_cancelled", NOW + timedelta(seconds=1))

    assert completed.status is RunStatus.COMPLETED
    assert prestart_invalid.started_at is None
    assert prestart_invalid.activating_worker_boot_id is None
    with pytest.raises(RunTransitionError, match="completed"):
        completed.invalidate("late_failure", NOW + timedelta(seconds=2))


def test_run_lifecycle_rejects_prebound_or_partially_bound_activation_identity() -> None:
    standby = _run()
    active = standby.activate(
        command_id=UUID(int=21),
        worker_boot_id=UUID(int=22),
        started_at=NOW + timedelta(seconds=1),
    )
    invalidated = active.invalidate("worker_failed", NOW + timedelta(seconds=2))

    with pytest.raises(ValueError, match="standby run lifecycle"):
        replace(standby, requested_operator_command_id=UUID(int=21))
    with pytest.raises(ValueError, match="invalidated run lifecycle"):
        replace(invalidated, requested_operator_command_id=None)


def test_service_boot_identity_is_immutable_and_heartbeat_is_strictly_monotonic() -> None:
    instance = ServiceInstance.register(
        _runtime_identity(),
        run_id=UUID(int=11),
        first_seen_at=NOW,
    )
    heartbeat = instance.heartbeat(sequence=1, heartbeat_at=NOW + timedelta(seconds=1))

    assert heartbeat.heartbeat_sequence == 1
    assert heartbeat.heartbeat(sequence=1, heartbeat_at=heartbeat.last_heartbeat_at) is heartbeat
    with pytest.raises(ServiceInstanceConflict, match="sequence"):
        heartbeat.heartbeat(sequence=1, heartbeat_at=NOW + timedelta(seconds=2))
    with pytest.raises(ServiceInstanceConflict, match="regress"):
        heartbeat.heartbeat(sequence=2, heartbeat_at=NOW)

    stopped = heartbeat.stop("clean_shutdown", NOW + timedelta(seconds=2))
    assert stopped.terminal_reason == "clean_shutdown"
    with pytest.raises(ServiceInstanceConflict, match="stopped"):
        stopped.heartbeat(sequence=2, heartbeat_at=NOW + timedelta(seconds=3))


def test_runtime_identity_rejects_missing_fields_and_non_utc_time() -> None:
    with pytest.raises(ValueError, match="project_id"):
        _runtime_identity(project_id="")
    with pytest.raises(ValueError, match="UTC-aware"):
        _runtime_identity(started_at=NOW.replace(tzinfo=None))


def _descriptor() -> CandidateDescriptor:
    return CandidateDescriptor.build(
        git_sha="a" * 40,
        source_clean=True,
        uv_lock_sha256="b" * 64,
        dashboard_lock_sha256="c" * 64,
        schema_revision="0020",
        agent_implementation_hashes={
            name: f"{index + 1:064x}" for index, name in enumerate(ALL_AGENTS)
        },
        dashboard_asset_manifest_sha256="d" * 64,
        build_definition_sha256="e" * 64,
    )


def _run() -> PlatformRun:
    return PlatformRun.create(
        run_id=UUID(int=11),
        experiment_id=UUID(int=12),
        candidate_hash="a" * 64,
        manifest_hash="b" * 64,
        database_system_identifier="7669409277984608290",
        railway_environment_id="environment-1",
        purpose=RunPurpose.SOAK,
        created_at=NOW,
    )


def _runtime_identity(**overrides: object) -> RailwayRuntimeIdentity:
    values: dict[str, object] = {
        "project_id": "project-1",
        "environment_id": "environment-1",
        "service_id": "service-1",
        "deployment_id": "deployment-1",
        "snapshot_id": None,
        "replica_id": "replica-1",
        "region": "europe-west4",
        "service_role": ServiceRole.WORKER,
        "boot_id": UUID(int=22),
        "candidate_hash": "a" * 64,
        "started_at": NOW,
    }
    values.update(overrides)
    return RailwayRuntimeIdentity(**values)  # type: ignore[arg-type]
