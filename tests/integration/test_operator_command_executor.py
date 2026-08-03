from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from maais.db.replay import verify_ledger_consistency
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ExperimentStatus
from maais.operations.incidents import IncidentSeverity, IncidentState, IncidentStatus
from maais.operations.operator_commands import CommandStatus, CommandType, OperatorCommand
from maais.orchestration.operator_control import (
    FlattenPlan,
    FlattenPlanningError,
    OperatorCommandExecutor,
)
from tests.unit.experiments.test_runtime_policy import _live_manifest

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 3, 10, tzinfo=timezone.utc)
EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
COMMAND_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKER_ID = UUID("66666666-6666-4666-8666-666666666666")


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def _request(
    command_type: CommandType,
    *,
    command_id: UUID = COMMAND_ID,
    idempotency_key: str = "33333333-3333-4333-8333-333333333333",
    payload: dict[str, object] | None = None,
):
    confirmation = (
        None
        if command_type is CommandType.ACKNOWLEDGE_INCIDENT
        else f"CONFIRM {command_type.value.upper()}"
    )
    return OperatorCommand.request(
        command_id=command_id,
        experiment_id=EXPERIMENT_ID,
        command_type=command_type,
        idempotency_key=idempotency_key,
        actor="local_operator",
        reason="operator requested a controlled state change",
        payload=payload or {},
        confirmation=confirmation,
        requested_at=NOW,
    )


async def _running_experiment(uow_factory: UnitOfWork):
    manifest = _live_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.experiments.ensure_running(manifest, started_at=NOW)
        await uow.controls.initialize(
            EXPERIMENT_ID,
            initialized_at=NOW,
            actor=f"paper_worker:{WORKER_ID}",
        )
    return manifest


async def test_pause_command_atomically_halts_entries_transitions_run_and_completes(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    requested = _request(CommandType.PAUSE)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)
        consistency = await verify_ledger_consistency(uow.session)

    assert execution is not None
    assert execution.command == stored
    assert execution.stop_worker is False
    assert stored.status is CommandStatus.COMPLETED
    assert stored.accepted_by == f"paper_worker:{WORKER_ID}"
    assert stored.result == {
        "command": "pause",
        "experiment_status": "paused",
        "kill_switch_active": True,
        "control_version": 2,
    }
    assert status is ExperimentStatus.PAUSED
    assert control.kill_switch_active
    assert control.reason == f"operator_pause:{COMMAND_ID}"
    assert consistency.ok


async def test_emergency_halt_is_persistent_and_keeps_the_worker_available(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    requested = _request(CommandType.EMERGENCY_HALT)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and not execution.stop_worker
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "emergency_halt",
        "experiment_status": "paused",
        "kill_switch_active": True,
        "control_version": 2,
    }
    assert status is ExperimentStatus.PAUSED
    assert control.reason == f"operator_emergency_halt:{COMMAND_ID}"


async def test_resume_clears_only_the_matching_operator_pause_and_reopens_entries(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    clock = _Clock()
    executor = OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=clock,
    )
    pause = _request(CommandType.PAUSE)
    resume_id = UUID("77777777-7777-4777-8777-777777777777")
    resume = _request(
        CommandType.RESUME,
        command_id=resume_id,
        idempotency_key="88888888-8888-4888-8888-888888888888",
        payload={"expected_control_version": 2},
    )
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(pause)
    await executor.execute_next()
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(resume)

    execution = await executor.execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(resume_id)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "resume",
        "experiment_status": "running",
        "kill_switch_active": False,
        "control_version": 3,
    }
    assert status is ExperimentStatus.RUNNING
    assert not control.kill_switch_active
    assert control.reason is None


async def test_resume_rejects_emergency_halt_without_weakening_the_control(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    clock = _Clock()
    executor = OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=clock,
    )
    halt = _request(CommandType.EMERGENCY_HALT)
    resume_id = UUID("77777777-7777-4777-8777-777777777777")
    resume = _request(
        CommandType.RESUME,
        command_id=resume_id,
        idempotency_key="88888888-8888-4888-8888-888888888888",
        payload={"expected_control_version": 2},
    )
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(halt)
    await executor.execute_next()
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(resume)

    execution = await executor.execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(resume_id)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.REJECTED
    assert stored.result == {
        "reason_code": "resume_requires_operator_pause",
        "detail": "resume cannot clear an emergency or system halt",
    }
    assert status is ExperimentStatus.PAUSED
    assert control.kill_switch_active
    assert control.reason == f"operator_emergency_halt:{COMMAND_ID}"


async def test_stop_requires_flat_state_then_marks_worker_for_graceful_shutdown(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    requested = _request(CommandType.STOP)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and execution.stop_worker
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "stop",
        "experiment_status": "stopped",
        "kill_switch_active": True,
        "control_version": 2,
        "open_positions": 0,
        "pending_orders": 0,
    }
    assert status is ExperimentStatus.STOPPED
    assert control.reason == f"operator_stop:{COMMAND_ID}"


async def test_failed_command_application_rolls_back_acceptance_and_partial_control(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    requested = _request(CommandType.PAUSE)
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.controls.initialize(
            EXPERIMENT_ID,
            initialized_at=NOW,
            actor=f"paper_worker:{WORKER_ID}",
        )
        await uow.commands.enqueue(requested)

    with pytest.raises(RuntimeError, match="cannot pause experiment from created"):
        await OperatorCommandExecutor(
            uow=uow_factory,
            manifest=manifest,
            worker_id=WORKER_ID,
            now=_Clock(),
        ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        control = await uow.controls.current(EXPERIMENT_ID)
        command_events = await uow.events.load_stream(COMMAND_ID, "operator_command")

    assert stored.status is CommandStatus.REQUESTED
    assert stored.accepted_by is None
    assert not control.kill_switch_active
    assert [event.event_type for event in command_events] == ["operator_command.requested"]


async def test_incident_acknowledgement_is_applied_by_worker_and_fully_audited(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    incident_id = UUID("99999999-9999-4999-8999-999999999999")
    incident = IncidentState.create(
        incident_id=incident_id,
        experiment_id=EXPERIMENT_ID,
        deduplication_key="test:operator-command-acknowledgement",
        severity=IncidentSeverity.ERROR,
        component="test",
        reason_code="operator_review_required",
        evidence={"source": "integration_test"},
        requires_operator_review=True,
        detected_at=NOW,
    )
    requested = _request(
        CommandType.ACKNOWLEDGE_INCIDENT,
        payload={"incident_id": str(incident_id)},
    )
    async with uow_factory.begin() as uow:
        await uow.incidents.record(incident)
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        changed = await uow.incidents.get(incident_id)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "acknowledge_incident",
        "incident_id": str(incident_id),
        "incident_status": "acknowledged",
        "incident_version": 2,
    }
    assert changed.status is IncidentStatus.ACKNOWLEDGED
    assert changed.acknowledged_by == "local_operator"


async def test_confirmed_incident_resolution_records_operator_text_and_result(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    incident_id = UUID("99999999-9999-4999-8999-999999999999")
    incident = IncidentState.create(
        incident_id=incident_id,
        experiment_id=EXPERIMENT_ID,
        deduplication_key="test:operator-command-resolution",
        severity=IncidentSeverity.ERROR,
        component="test",
        reason_code="operator_review_required",
        evidence={"source": "integration_test"},
        requires_operator_review=True,
        detected_at=NOW,
    )
    resolution = "reviewed the immutable evidence and confirmed recovery"
    requested = _request(
        CommandType.RESOLVE_INCIDENT,
        payload={"incident_id": str(incident_id), "resolution": resolution},
    )
    async with uow_factory.begin() as uow:
        await uow.incidents.record(incident)
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        changed = await uow.incidents.get(incident_id)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "resolve_incident",
        "incident_id": str(incident_id),
        "incident_status": "resolved",
        "incident_version": 2,
    }
    assert changed.status is IncidentStatus.RESOLVED
    assert changed.resolved_by == "local_operator"
    assert changed.resolution == resolution


async def test_kill_switch_reset_requires_exact_reviewed_state_and_stays_paused(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    clock = _Clock()
    executor = OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=clock,
    )
    halt = _request(CommandType.EMERGENCY_HALT)
    reset_id = UUID("77777777-7777-4777-8777-777777777777")
    expected_reason = f"operator_emergency_halt:{COMMAND_ID}"
    reset = _request(
        CommandType.RESET_KILL_SWITCH,
        command_id=reset_id,
        idempotency_key="88888888-8888-4888-8888-888888888888",
        payload={
            "expected_control_version": 2,
            "expected_reason": expected_reason,
        },
    )
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(halt)
    await executor.execute_next()
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(reset)

    execution = await executor.execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(reset_id)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "reset_kill_switch",
        "experiment_status": "paused",
        "kill_switch_active": False,
        "control_version": 3,
        "open_positions": 0,
        "pending_orders": 0,
        "blocking_recoveries": 0,
        "blocking_incidents": 0,
    }
    assert status is ExperimentStatus.PAUSED
    assert not control.kill_switch_active
    assert control.reason is None


async def test_resume_after_explicit_kill_switch_reset_transitions_without_new_reset(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    clock = _Clock()
    executor = OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=clock,
    )
    halt = _request(CommandType.EMERGENCY_HALT)
    reset_id = UUID("77777777-7777-4777-8777-777777777777")
    resume_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    reset = _request(
        CommandType.RESET_KILL_SWITCH,
        command_id=reset_id,
        idempotency_key="88888888-8888-4888-8888-888888888888",
        payload={
            "expected_control_version": 2,
            "expected_reason": f"operator_emergency_halt:{COMMAND_ID}",
        },
    )
    resume = _request(
        CommandType.RESUME,
        command_id=resume_id,
        idempotency_key="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        payload={"expected_control_version": 3},
    )
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(halt)
    await executor.execute_next()
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(reset)
    await executor.execute_next()
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(resume)

    execution = await executor.execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(resume_id)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "resume",
        "experiment_status": "running",
        "kill_switch_active": False,
        "control_version": 3,
    }
    assert status is ExperimentStatus.RUNNING
    assert control.version == 3


async def test_start_command_transitions_prepared_experiment_and_requests_activation(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017")
    requested = _request(CommandType.START)
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.controls.initialize(
            EXPERIMENT_ID,
            initialized_at=NOW,
            actor=f"paper_worker:{WORKER_ID}",
        )
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None
    assert execution.activate_worker
    assert not execution.stop_worker
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "start",
        "experiment_status": "running",
        "kill_switch_active": False,
        "control_version": 1,
    }
    assert status is ExperimentStatus.RUNNING
    assert not control.kill_switch_active


async def test_flatten_first_phase_durably_pauses_before_waiting_for_liquidity(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    requested = _request(CommandType.FLATTEN)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(requested)

    execution = await OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
    ).execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None
    assert execution.command == stored
    assert execution.command.status is CommandStatus.ACCEPTED
    assert not execution.stop_worker
    assert not execution.activate_worker
    assert status is ExperimentStatus.PAUSED
    assert control.kill_switch_active
    assert control.reason == f"operator_flatten:{COMMAND_ID}"


async def test_flatten_of_already_flat_account_completes_with_full_noop_metadata(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    requested = _request(CommandType.FLATTEN)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(requested)
        account = await uow.paper_execution.load_account(EXPERIMENT_ID)

    class _Planner:
        async def prepare(self, command: OperatorCommand) -> FlattenPlan:
            return FlattenPlan(
                command_id=command.command_id,
                source_account=account,
                executions=(),
                planned_at=NOW + timedelta(seconds=10),
            )

    executor = OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
        flatten_planner=_Planner(),
    )
    first = await executor.execute_next()

    second = await executor.execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert first is not None and first.command.status is CommandStatus.ACCEPTED
    assert second is not None and second.command == stored
    assert stored.status is CommandStatus.COMPLETED
    assert stored.result == {
        "command": "flatten",
        "experiment_status": "paused",
        "kill_switch_active": True,
        "control_version": 2,
        "source_account_version": 0,
        "final_account_version": 0,
        "flattened_positions": 0,
        "orders": (),
        "planned_at": "2026-08-03T10:00:10Z",
    }
    assert control.reason == f"operator_flatten:{COMMAND_ID}"


async def test_unfillable_flatten_is_rejected_but_keeps_the_safety_halt(
    uow_factory: UnitOfWork,
) -> None:
    manifest = await _running_experiment(uow_factory)
    requested = _request(CommandType.FLATTEN)
    async with uow_factory.begin() as uow:
        await uow.commands.enqueue(requested)

    class _Planner:
        async def prepare(self, command: OperatorCommand) -> FlattenPlan:
            raise FlattenPlanningError(
                "flatten_eligible_book_timeout",
                "no eligible BTCUSDT book arrived before the paper order TTL",
            )

    executor = OperatorCommandExecutor(
        uow=uow_factory,
        manifest=manifest,
        worker_id=WORKER_ID,
        now=_Clock(),
        flatten_planner=_Planner(),
    )
    await executor.execute_next()

    execution = await executor.execute_next()

    async with uow_factory.begin() as uow:
        stored = await uow.commands.get(COMMAND_ID)
        status = await uow.experiments.get_status(EXPERIMENT_ID)
        control = await uow.controls.current(EXPERIMENT_ID)

    assert execution is not None and execution.command == stored
    assert stored.status is CommandStatus.REJECTED
    assert stored.result == {
        "reason_code": "flatten_eligible_book_timeout",
        "detail": "no eligible BTCUSDT book arrived before the paper order TTL",
    }
    assert status is ExperimentStatus.PAUSED
    assert control.kill_switch_active
    assert control.reason == f"operator_flatten:{COMMAND_ID}"
