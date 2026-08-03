"""Crash-resumable, causal emergency flatten planning for local paper positions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from maais.db.unit_of_work import UnitOfWork
from maais.execution.paper.account import AccountState
from maais.execution.paper.broker import ExitExecutionHalt, MarketExitCommand, PaperBroker
from maais.execution.paper.clock import require_utc
from maais.execution.paper.exits import ExitPlan, ExitPlanStatus
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.operations.operator_commands import CommandStatus, CommandType, OperatorCommand
from maais.orchestration.observations import EligibleBookTimeout, MarketObservationBuffer
from maais.orchestration.operator_control import (
    FlattenPlan,
    FlattenPlanningError,
    FlattenTrigger,
)


@dataclass(frozen=True, slots=True)
class FlattenSource:
    account: AccountState
    exit_plans: tuple[ExitPlan, ...]
    entry_proposal_ids: Mapping[UUID, UUID]
    pending_order_count: int

    def __post_init__(self) -> None:
        if self.pending_order_count < 0:
            raise ValueError("flatten pending order count cannot be negative")
        object.__setattr__(
            self,
            "entry_proposal_ids",
            MappingProxyType(dict(self.entry_proposal_ids)),
        )


class FlattenSourceLoader(Protocol):
    async def load(self, command: OperatorCommand) -> FlattenSource: ...


class PostgresFlattenSourceLoader:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def load(self, command: OperatorCommand) -> FlattenSource:
        async with self._uow.begin() as transaction:
            account = await transaction.paper_execution.load_account(command.experiment_id)
            exit_plans = await transaction.paper_execution.load_open_exit_plans(
                command.experiment_id
            )
            pending_orders = await transaction.paper_execution.load_pending_orders(
                command.experiment_id
            )
            entry_proposal_ids = {
                position.position_id: (
                    await transaction.paper_execution.load_position_entry_proposal_id(
                        command.experiment_id,
                        position.position_id,
                    )
                )
                for position in account.positions.values()
                if not position.is_flat
            }
        return FlattenSource(
            account=account,
            exit_plans=exit_plans,
            entry_proposal_ids=entry_proposal_ids,
            pending_order_count=len(pending_orders),
        )


class LivePaperFlattenPlanner:
    """Prepare deterministic reduce-only fills from causal marks and later books."""

    def __init__(
        self,
        *,
        manifest: ExperimentManifest,
        policy: LivePaperPolicy,
        source_loader: FlattenSourceLoader,
        observations: MarketObservationBuffer,
        broker: PaperBroker,
        exchange_filters: Mapping[str, ExchangeFilterSnapshot],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        filters = dict(exchange_filters)
        if set(filters) != set(manifest.symbols):
            raise ValueError("flatten filters must cover exact manifest symbols")
        if any(symbol != snapshot.symbol for symbol, snapshot in filters.items()):
            raise ValueError("flatten filters and symbols differ")
        self._manifest = manifest
        self._policy = policy
        self._source_loader = source_loader
        self._observations = observations
        self._broker = broker
        self._filters = filters
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def prepare(self, command: OperatorCommand) -> FlattenPlan:
        if (
            command.experiment_id != self._manifest.experiment_id
            or command.command_type is not CommandType.FLATTEN
            or command.status is not CommandStatus.ACCEPTED
        ):
            raise ValueError("flatten planner requires the accepted manifest command")
        planned_at = self._now()
        require_utc(planned_at, "flatten planned_at")
        source = await self._source_loader.load(command)
        if source.account.experiment_id != self._manifest.experiment_id:
            raise FlattenPlanningError(
                "flatten_account_scope_mismatch",
                "paper account does not belong to the flatten experiment",
            )
        if source.pending_order_count:
            raise FlattenPlanningError(
                "flatten_pending_orders",
                "flatten requires zero pending paper orders",
            )
        positions = tuple(
            sorted(
                (
                    position
                    for position in source.account.positions.values()
                    if not position.is_flat
                ),
                key=lambda item: (item.symbol, item.position_id),
            )
        )
        if not positions:
            return FlattenPlan(
                command_id=command.command_id,
                source_account=source.account,
                executions=(),
                planned_at=planned_at,
            )
        plans_by_position = {plan.position_id: plan for plan in source.exit_plans}
        if len(plans_by_position) != len(source.exit_plans) or set(plans_by_position) != {
            position.position_id for position in positions
        }:
            raise FlattenPlanningError(
                "flatten_exit_plan_mismatch",
                "every open paper position requires exactly one exit plan",
            )

        inputs: list[tuple[object, ExitPlan, UUID, FlattenTrigger]] = []
        for position in positions:
            exit_plan = plans_by_position[position.position_id]
            if exit_plan.status is not ExitPlanStatus.ACTIVE:
                raise FlattenPlanningError(
                    "flatten_exit_already_triggered",
                    "flatten requires an active, untriggered exit plan",
                )
            proposal_id = source.entry_proposal_ids.get(position.position_id)
            if proposal_id is None:
                raise FlattenPlanningError(
                    "flatten_missing_entry_lineage",
                    "open position has no original entry proposal lineage",
                )
            latest_mark = self._observations.latest_mark(
                position.symbol,
                at_or_before=planned_at,
            )
            if latest_mark is None:
                raise FlattenPlanningError(
                    "flatten_missing_causal_mark",
                    f"no causal mark is available for {position.symbol}",
                )
            mark_price, mark_event = latest_mark
            eligible_after = planned_at + self._policy.execution_latency
            inputs.append(
                (
                    position,
                    exit_plan,
                    proposal_id,
                    FlattenTrigger(
                        symbol=position.symbol,
                        position_id=position.position_id,
                        exit_plan_id=exit_plan.plan_id,
                        mark_event_id=mark_event.event_id,
                        mark_observed_at=mark_event.observed_at,
                        mark_price=mark_price,
                        eligible_after=eligible_after,
                    ),
                )
            )
        try:
            books_by_position = await asyncio.gather(
                *(
                    self._observations.books_after(
                        trigger.symbol,
                        trigger.eligible_after,
                        timeout=self._policy.book_wait_timeout,
                    )
                    for _, _, _, trigger in inputs
                )
            )
        except EligibleBookTimeout as exc:
            raise FlattenPlanningError(
                "flatten_eligible_book_timeout",
                str(exc),
            ) from exc

        account = source.account
        executions = []
        triggers = []
        for (_, exit_plan, proposal_id, trigger), books in zip(
            inputs,
            books_by_position,
            strict=True,
        ):
            evaluation = exit_plan.emergency_flatten(
                planned_at,
                executable_price=trigger.mark_price,
            )
            if evaluation.intent is None:
                raise RuntimeError("emergency flatten did not produce an exit intent")
            exit_command = MarketExitCommand(
                order_id=_id("flatten-order", command.command_id, trigger.position_id),
                fill_id=_id("flatten-fill", command.command_id, trigger.position_id),
                experiment_id=self._manifest.experiment_id,
                proposal_id=proposal_id,
                client_order_id=(f"paper-flatten-{command.command_id}-{trigger.position_id}"),
                symbol=trigger.symbol,
                decision_executable_price=trigger.mark_price,
                execution_latency=self._policy.execution_latency,
                created_at=planned_at,
                expires_at=planned_at + self._policy.proposal_ttl,
                taker_fee_rate=self._policy.taker_fee_rate,
                intent=evaluation.intent,
                exchange_filters=self._filters[trigger.symbol],
            )
            try:
                result = self._broker.execute_market_exit(
                    exit_command,
                    account=account,
                    exit_plan=evaluation.plan,
                    books=books,
                )
            except ExitExecutionHalt as exc:
                market_event = exc.market_event_id or "none"
                raise FlattenPlanningError(
                    "flatten_exit_unfillable",
                    f"{trigger.symbol} exit was unfillable: {exc.reason}; "
                    f"market_event_id={market_event}",
                ) from exc
            if result.record.account is None:
                raise RuntimeError("flatten execution did not produce an account")
            account = result.record.account
            executions.append(result.record)
            triggers.append(trigger)
        return FlattenPlan(
            command_id=command.command_id,
            source_account=source.account,
            executions=tuple(executions),
            planned_at=planned_at,
            triggers=tuple(triggers),
        )


def _id(namespace: str, command_id: UUID, position_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"maais://{namespace}/{command_id}/{position_id}",
    )
