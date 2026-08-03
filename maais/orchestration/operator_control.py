"""Worker-owned application of queued local operator commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ExperimentStatus
from maais.execution.paper.account import AccountState
from maais.execution.paper.clock import require_utc
from maais.execution.paper.records import PaperExecutionRecord
from maais.experiments.manifest import ExperimentManifest
from maais.operations.operator_commands import (
    CommandStatus,
    CommandType,
    OperatorCommand,
)


@dataclass(frozen=True, slots=True)
class FlattenTrigger:
    symbol: str
    position_id: UUID
    exit_plan_id: UUID
    mark_event_id: str
    mark_observed_at: datetime
    mark_price: Decimal
    eligible_after: datetime

    def __post_init__(self) -> None:
        if not self.symbol or not self.mark_event_id:
            raise ValueError("flatten trigger identity is required")
        if self.position_id.int == 0 or self.exit_plan_id.int == 0:
            raise ValueError("flatten trigger position identities cannot be nil")
        require_utc(self.mark_observed_at, "flatten mark_observed_at")
        require_utc(self.eligible_after, "flatten eligible_after")
        if self.eligible_after <= self.mark_observed_at:
            raise ValueError("flatten liquidity eligibility must follow the causal mark")
        if not self.mark_price.is_finite() or self.mark_price <= 0:
            raise ValueError("flatten mark price must be positive and finite")


@dataclass(frozen=True, slots=True)
class FlattenPlan:
    command_id: UUID
    source_account: AccountState
    executions: tuple[PaperExecutionRecord, ...]
    planned_at: datetime
    triggers: tuple[FlattenTrigger, ...] = ()

    def __post_init__(self) -> None:
        if self.command_id.int == 0:
            raise ValueError("flatten plan command identity cannot be nil")
        require_utc(self.planned_at, "flatten planned_at")
        if len(self.triggers) != len(self.executions):
            raise ValueError("flatten triggers and executions must align")
        account = self.source_account
        for trigger, execution in zip(self.triggers, self.executions, strict=True):
            execution.validate()
            if execution.account is None:
                raise ValueError("flatten execution requires an account result")
            if execution.order.experiment_id != account.experiment_id:
                raise ValueError("flatten execution belongs to another experiment")
            if (
                execution.order.symbol != trigger.symbol
                or execution.exit_plan is None
                or execution.exit_plan.plan_id != trigger.exit_plan_id
                or execution.exit_plan.position_id != trigger.position_id
            ):
                raise ValueError("flatten trigger and execution identities differ")
            if execution.account.version != account.version + 1:
                raise ValueError("flatten execution account versions are not contiguous")
            account = execution.account
        if any(not position.is_flat for position in account.positions.values()):
            raise ValueError("flatten plan must close every open position")


class FlattenPlanner(Protocol):
    async def prepare(self, command: OperatorCommand) -> FlattenPlan: ...


class FlattenPlanningError(RuntimeError):
    def __init__(self, reason_code: str, detail: str) -> None:
        if not reason_code or not detail:
            raise ValueError("flatten planning error requires a code and detail")
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class OperatorCommandExecution:
    command: OperatorCommand
    stop_worker: bool
    activate_worker: bool = False


class OperatorCommandExecutor:
    """Claim, apply, and finish one command inside one database transaction."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        manifest: ExperimentManifest,
        worker_id: UUID,
        now: Callable[[], datetime] | None = None,
        flatten_planner: FlattenPlanner | None = None,
    ) -> None:
        if worker_id.int == 0:
            raise ValueError("operator command worker identity cannot be nil")
        self._uow = uow
        self._manifest = manifest
        self._worker_id = worker_id
        self._worker_name = f"paper_worker:{worker_id}"
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._flatten_planner = flatten_planner

    async def execute_next(self) -> OperatorCommandExecution | None:
        if self._flatten_planner is not None:
            async with self._uow.begin() as transaction:
                accepted = await transaction.commands.list_for_experiment(
                    self._manifest.experiment_id,
                    status=CommandStatus.ACCEPTED,
                )
            accepted_flatten = tuple(
                command for command in accepted if command.command_type is CommandType.FLATTEN
            )
            if len(accepted_flatten) > 1:
                raise RuntimeError("multiple accepted flatten commands violate serialization")
            if accepted_flatten:
                async with self._uow.begin() as transaction:
                    command = await transaction.commands.claim_next(
                        self._manifest.experiment_id,
                        worker_id=self._worker_name,
                        accepted_at=self._now(),
                    )
                    if command is None or command.command_id != accepted_flatten[0].command_id:
                        raise RuntimeError("accepted flatten command changed during recovery")
                return await self._complete_flatten(command)
        async with self._uow.begin() as transaction:
            command = await transaction.commands.claim_next(
                self._manifest.experiment_id,
                worker_id=self._worker_name,
                accepted_at=self._now(),
            )
            if command is None:
                return None
            if command.command_type is CommandType.FLATTEN:
                flatten_reason = f"operator_flatten:{command.command_id}"
                control = await transaction.controls.current(self._manifest.experiment_id)
                if not (control.kill_switch_active and control.reason == flatten_reason):
                    await transaction.controls.halt(
                        self._manifest.experiment_id,
                        reason=flatten_reason,
                        halted_at=self._now(),
                        actor=self._worker_name,
                    )
                    await transaction.experiments.pause_active(
                        self._manifest,
                        paused_at=self._now(),
                    )
                return OperatorCommandExecution(command=command, stop_worker=False)
            if command.command_type is CommandType.START:
                control = await transaction.controls.current(self._manifest.experiment_id)
                if control.kill_switch_active:
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="start_blocked_by_kill_switch",
                        detail="start requires an inactive persistent kill switch",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                started = await transaction.experiments.ensure_running(
                    self._manifest,
                    started_at=self._now(),
                )
                completed = await transaction.commands.complete(
                    command.command_id,
                    worker_id=self._worker_name,
                    completed_at=self._now(),
                    result={
                        "command": command.command_type.value,
                        "experiment_status": "running",
                        "kill_switch_active": control.kill_switch_active,
                        "control_version": control.version,
                    },
                )
                return OperatorCommandExecution(
                    command=completed,
                    stop_worker=False,
                    activate_worker=started,
                )
            if command.command_type in {
                CommandType.ACKNOWLEDGE_INCIDENT,
                CommandType.RESOLVE_INCIDENT,
            }:
                raw_incident_id = command.payload.get("incident_id")
                try:
                    incident_id = UUID(str(raw_incident_id))
                except (TypeError, ValueError, AttributeError):
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="invalid_command_payload",
                        detail="incident action requires a valid incident_id",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                incident = await transaction.incidents.get(incident_id)
                if incident.experiment_id != self._manifest.experiment_id:
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="incident_scope_mismatch",
                        detail="incident does not belong to the command experiment",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                if command.command_type is CommandType.ACKNOWLEDGE_INCIDENT:
                    changed = incident.acknowledge(command.actor, self._now())
                else:
                    resolution = command.payload.get("resolution")
                    if not isinstance(resolution, str) or not resolution.strip():
                        rejected = await transaction.commands.reject(
                            command.command_id,
                            worker_id=self._worker_name,
                            completed_at=self._now(),
                            reason_code="invalid_command_payload",
                            detail="incident resolution requires nonempty resolution text",
                        )
                        return OperatorCommandExecution(
                            command=rejected,
                            stop_worker=False,
                        )
                    changed = incident.resolve(
                        command.actor,
                        resolution.strip(),
                        self._now(),
                        operator_confirmed=command.operator_confirmed,
                    )
                await transaction.incidents.record(changed)
                completed = await transaction.commands.complete(
                    command.command_id,
                    worker_id=self._worker_name,
                    completed_at=self._now(),
                    result={
                        "command": command.command_type.value,
                        "incident_id": str(incident_id),
                        "incident_status": changed.status.value,
                        "incident_version": changed.version,
                    },
                )
                return OperatorCommandExecution(command=completed, stop_worker=False)
            if command.command_type is CommandType.STOP:
                account = await transaction.paper_execution.load_account(
                    self._manifest.experiment_id
                )
                open_positions = sum(
                    1 for position in account.positions.values() if not position.is_flat
                )
                pending_orders = await transaction.paper_execution.load_pending_orders(
                    self._manifest.experiment_id
                )
                if open_positions or pending_orders:
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="open_state_requires_flatten",
                        detail="stop requires zero open positions and pending paper orders",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                control = await transaction.controls.halt(
                    self._manifest.experiment_id,
                    reason=f"operator_stop:{command.command_id}",
                    halted_at=self._now(),
                    actor=self._worker_name,
                )
                await transaction.experiments.stop_active(
                    self._manifest,
                    stopped_at=self._now(),
                )
                completed = await transaction.commands.complete(
                    command.command_id,
                    worker_id=self._worker_name,
                    completed_at=self._now(),
                    result={
                        "command": command.command_type.value,
                        "experiment_status": "stopped",
                        "kill_switch_active": control.kill_switch_active,
                        "control_version": control.version,
                        "open_positions": open_positions,
                        "pending_orders": len(pending_orders),
                    },
                )
                return OperatorCommandExecution(command=completed, stop_worker=True)
            if command.command_type is CommandType.RESET_KILL_SWITCH:
                expected_version = command.payload.get("expected_control_version")
                expected_reason = command.payload.get("expected_reason")
                if (
                    not isinstance(expected_version, int)
                    or isinstance(expected_version, bool)
                    or not isinstance(expected_reason, str)
                    or not expected_reason
                ):
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="invalid_command_payload",
                        detail=(
                            "kill-switch reset requires expected_control_version and "
                            "expected_reason"
                        ),
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                experiment_status = await transaction.experiments.get_status(
                    self._manifest.experiment_id
                )
                current_control = await transaction.controls.current(self._manifest.experiment_id)
                account = await transaction.paper_execution.load_account(
                    self._manifest.experiment_id
                )
                open_positions = sum(
                    1 for position in account.positions.values() if not position.is_flat
                )
                pending_orders = await transaction.paper_execution.load_pending_orders(
                    self._manifest.experiment_id
                )
                recoveries = await transaction.market_data.get_blocking_recoveries(
                    self._manifest.experiment_id
                )
                unresolved = await transaction.incidents.get_unresolved(
                    self._manifest.experiment_id
                )
                blocking_incidents = tuple(
                    incident
                    for incident in unresolved
                    if incident.requires_operator_review or incident.severity.value == "critical"
                )
                exact_state = (
                    current_control.kill_switch_active
                    and current_control.version == expected_version
                    and current_control.reason == expected_reason
                )
                if (
                    experiment_status is not ExperimentStatus.PAUSED
                    or not exact_state
                    or open_positions
                    or pending_orders
                    or recoveries
                    or blocking_incidents
                ):
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="unsafe_kill_switch_reset",
                        detail=(
                            "reset requires exact paused control state, flat account, no "
                            "pending orders, recoveries, or unresolved review incidents"
                        ),
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                control = await transaction.controls.reset(
                    self._manifest.experiment_id,
                    reset_at=self._now(),
                    actor=self._worker_name,
                    expected_version=expected_version,
                    allowed_reason_prefix=expected_reason,
                )
                completed = await transaction.commands.complete(
                    command.command_id,
                    worker_id=self._worker_name,
                    completed_at=self._now(),
                    result={
                        "command": command.command_type.value,
                        "experiment_status": experiment_status.value,
                        "kill_switch_active": control.kill_switch_active,
                        "control_version": control.version,
                        "open_positions": open_positions,
                        "pending_orders": len(pending_orders),
                        "blocking_recoveries": len(recoveries),
                        "blocking_incidents": len(blocking_incidents),
                    },
                )
                return OperatorCommandExecution(command=completed, stop_worker=False)
            if command.command_type is CommandType.RESUME:
                expected_version = command.payload.get("expected_control_version")
                if not isinstance(expected_version, int) or isinstance(expected_version, bool):
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="invalid_command_payload",
                        detail="resume requires integer expected_control_version",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                current_control = await transaction.controls.current(self._manifest.experiment_id)
                if current_control.version != expected_version:
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="control_version_changed",
                        detail="kill-switch state changed after operator confirmation",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                if current_control.kill_switch_active and not (
                    current_control.reason or ""
                ).startswith("operator_pause:"):
                    rejected = await transaction.commands.reject(
                        command.command_id,
                        worker_id=self._worker_name,
                        completed_at=self._now(),
                        reason_code="resume_requires_operator_pause",
                        detail="resume cannot clear an emergency or system halt",
                    )
                    return OperatorCommandExecution(command=rejected, stop_worker=False)
                if current_control.kill_switch_active:
                    control = await transaction.controls.reset(
                        self._manifest.experiment_id,
                        reset_at=self._now(),
                        actor=self._worker_name,
                        expected_version=expected_version,
                        allowed_reason_prefix="operator_pause:",
                    )
                else:
                    control = current_control
                await transaction.experiments.resume_paused(
                    self._manifest,
                    resumed_at=self._now(),
                )
                completed = await transaction.commands.complete(
                    command.command_id,
                    worker_id=self._worker_name,
                    completed_at=self._now(),
                    result={
                        "command": command.command_type.value,
                        "experiment_status": "running",
                        "kill_switch_active": control.kill_switch_active,
                        "control_version": control.version,
                    },
                )
                return OperatorCommandExecution(command=completed, stop_worker=False)
            if command.command_type not in {
                CommandType.PAUSE,
                CommandType.EMERGENCY_HALT,
            }:
                raise RuntimeError(
                    f"worker does not implement command: {command.command_type.value}"
                )
            reason_prefix = (
                "operator_pause"
                if command.command_type is CommandType.PAUSE
                else "operator_emergency_halt"
            )
            control = await transaction.controls.halt(
                self._manifest.experiment_id,
                reason=f"{reason_prefix}:{command.command_id}",
                halted_at=self._now(),
                actor=self._worker_name,
            )
            await transaction.experiments.pause_active(
                self._manifest,
                paused_at=self._now(),
            )
            completed = await transaction.commands.complete(
                command.command_id,
                worker_id=self._worker_name,
                completed_at=self._now(),
                result={
                    "command": command.command_type.value,
                    "experiment_status": "paused",
                    "kill_switch_active": control.kill_switch_active,
                    "control_version": control.version,
                },
            )
            return OperatorCommandExecution(command=completed, stop_worker=False)

    async def _complete_flatten(
        self,
        command: OperatorCommand,
    ) -> OperatorCommandExecution:
        if self._flatten_planner is None:
            raise RuntimeError("flatten planner is not configured")
        try:
            plan = await self._flatten_planner.prepare(command)
        except FlattenPlanningError as exc:
            async with self._uow.begin() as transaction:
                rejected = await transaction.commands.reject(
                    command.command_id,
                    worker_id=self._worker_name,
                    completed_at=self._now(),
                    reason_code=exc.reason_code,
                    detail=exc.detail,
                )
            return OperatorCommandExecution(command=rejected, stop_worker=False)
        if plan.command_id != command.command_id:
            raise RuntimeError("flatten planner returned a different command identity")
        flatten_reason = f"operator_flatten:{command.command_id}"
        async with self._uow.begin() as transaction:
            claimed = await transaction.commands.claim_next(
                self._manifest.experiment_id,
                worker_id=self._worker_name,
                accepted_at=self._now(),
            )
            if claimed is None or claimed.command_id != command.command_id:
                raise RuntimeError("flatten command changed before atomic persistence")
            control = await transaction.controls.current(self._manifest.experiment_id)
            status = await transaction.experiments.get_status(self._manifest.experiment_id)
            if (
                status is not ExperimentStatus.PAUSED
                or not control.kill_switch_active
                or control.reason != flatten_reason
            ):
                raise RuntimeError("flatten safety state changed before persistence")
            account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
            if account != plan.source_account:
                raise RuntimeError("paper account changed while flatten was being prepared")
            pending_orders = await transaction.paper_execution.load_pending_orders(
                self._manifest.experiment_id
            )
            if pending_orders:
                raise RuntimeError("flatten cannot proceed with pending paper orders")
            final_account = account
            for execution in plan.executions:
                await transaction.paper_execution.record(execution)
                assert execution.account is not None
                final_account = execution.account
            if any(not position.is_flat for position in final_account.positions.values()):
                raise RuntimeError("flatten persistence left an open paper position")
            orders = tuple(
                {
                    "order_id": str(execution.order.order_id),
                    "symbol": execution.order.symbol,
                    "side": execution.order.side.value,
                    "quantity": execution.order.quantity,
                    "fill_id": str(execution.fills[0].id),
                    "market_event_id": execution.fills[0].market_event_id,
                    "fill_at": execution.fills[0].fill_at,
                    "price": execution.fills[0].price,
                    "fee": execution.fills[0].fee,
                    "trigger_mark_event_id": trigger.mark_event_id,
                    "trigger_mark_observed_at": trigger.mark_observed_at,
                    "trigger_mark_price": trigger.mark_price,
                    "eligible_after": trigger.eligible_after,
                }
                for trigger, execution in zip(
                    plan.triggers,
                    plan.executions,
                    strict=True,
                )
            )
            completed = await transaction.commands.complete(
                command.command_id,
                worker_id=self._worker_name,
                completed_at=self._now(),
                result={
                    "command": command.command_type.value,
                    "experiment_status": status.value,
                    "kill_switch_active": control.kill_switch_active,
                    "control_version": control.version,
                    "source_account_version": account.version,
                    "final_account_version": final_account.version,
                    "flattened_positions": len(plan.executions),
                    "orders": orders,
                    "planned_at": plan.planned_at,
                },
            )
        return OperatorCommandExecution(command=completed, stop_worker=False)
