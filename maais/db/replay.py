from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.accounts import AccountSnapshotModel, ExitPlanModel, PositionModel
from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    GateEvaluationModel,
    TradeProposalModel,
)
from maais.db.models.execution import FillModel, OrderEventModel, OrderIntentModel
from maais.db.models.experiments import ExperimentModel
from maais.db.models.ledger import DomainEventModel, EventStreamModel, OutboxEventModel
from maais.domain.enums import ExperimentStatus
from maais.domain.json import content_hash


@dataclass(frozen=True, slots=True)
class LedgerConsistencyError:
    code: str
    aggregate_type: str | None
    aggregate_id: UUID | None
    details: str


@dataclass(frozen=True, slots=True)
class LedgerConsistencyReport:
    errors: tuple[LedgerConsistencyError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class RebuiltExperimentProjection:
    experiment_id: UUID
    status: ExperimentStatus
    started_at: datetime | None
    ended_at: datetime | None
    failure_reason: str | None
    config_hash: str
    manifest_hash: str

    def normalized(self) -> tuple[object, ...]:
        return (
            self.experiment_id,
            self.status.value,
            self.started_at,
            self.ended_at,
            self.failure_reason,
            self.config_hash,
            self.manifest_hash,
        )


async def rebuild_experiment_projection(
    session: AsyncSession,
    experiment_id: UUID,
) -> RebuiltExperimentProjection:
    events = (
        await session.scalars(
            select(DomainEventModel)
            .where(
                DomainEventModel.aggregate_type == "experiment",
                DomainEventModel.aggregate_id == experiment_id,
            )
            .order_by(DomainEventModel.stream_version)
        )
    ).all()
    if not events or events[0].event_type != "experiment.created":
        raise ValueError("experiment stream must begin with experiment.created")
    created = events[0]
    config_hash = str(created.payload_json.get("config_hash", ""))
    manifest_hash = str(created.payload_json.get("manifest_hash", ""))
    status = ExperimentStatus.CREATED
    started_at: datetime | None = None
    ended_at: datetime | None = None
    failure_reason: str | None = None
    event_statuses = {
        "experiment.started": ExperimentStatus.RUNNING,
        "experiment.paused": ExperimentStatus.PAUSED,
        "experiment.stopped": ExperimentStatus.STOPPED,
        "experiment.completed": ExperimentStatus.COMPLETED,
        "experiment.failed": ExperimentStatus.FAILED,
    }
    for event in events[1:]:
        target = event_statuses.get(event.event_type)
        if target is None:
            continue
        status = target
        if target is ExperimentStatus.RUNNING and started_at is None:
            started_at = event.occurred_at
        if target in {
            ExperimentStatus.STOPPED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
        }:
            ended_at = event.occurred_at
        if target is ExperimentStatus.FAILED:
            raw_reason = event.payload_json.get("failure_reason")
            failure_reason = str(raw_reason) if raw_reason is not None else None
    return RebuiltExperimentProjection(
        experiment_id=experiment_id,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        failure_reason=failure_reason,
        config_hash=config_hash,
        manifest_hash=manifest_hash,
    )


def _error(
    code: str,
    stream: EventStreamModel | None,
    details: str,
) -> LedgerConsistencyError:
    return LedgerConsistencyError(
        code=code,
        aggregate_type=stream.aggregate_type if stream is not None else None,
        aggregate_id=stream.aggregate_id if stream is not None else None,
        details=details,
    )


async def verify_ledger_consistency(session: AsyncSession) -> LedgerConsistencyReport:
    errors: list[LedgerConsistencyError] = []
    streams = (await session.scalars(select(EventStreamModel))).all()
    events = (
        await session.scalars(select(DomainEventModel).order_by(DomainEventModel.global_position))
    ).all()
    events_by_stream: dict[UUID, list[DomainEventModel]] = {}
    for event in events:
        events_by_stream.setdefault(event.stream_id, []).append(event)
    for stream in streams:
        stream_events = sorted(
            events_by_stream.get(stream.id, []),
            key=lambda event: event.stream_version,
        )
        actual_versions = [event.stream_version for event in stream_events]
        expected_versions = list(range(1, stream.current_version + 1))
        if actual_versions != expected_versions:
            errors.append(
                _error(
                    "stream_gap",
                    stream,
                    f"expected versions {expected_versions}, found {actual_versions}",
                )
            )
        for event in stream_events:
            if (
                event.aggregate_id != stream.aggregate_id
                or event.aggregate_type != stream.aggregate_type
            ):
                errors.append(
                    _error("stream_identity_mismatch", stream, f"event {event.id} identity differs")
                )

    outbox_rows = (await session.scalars(select(OutboxEventModel))).all()
    outbox_by_event = {row.domain_event_id: row for row in outbox_rows}
    for event in events:
        row = outbox_by_event.get(event.id)
        stream = next((item for item in streams if item.id == event.stream_id), None)
        if row is None:
            errors.append(_error("missing_outbox", stream, f"event {event.id} has no outbox row"))
            continue
        payload = row.payload_json
        expected_values = {
            "event_id": str(event.id),
            "global_position": event.global_position,
            "stream_version": event.stream_version,
            "event_type": event.event_type,
        }
        for key, expected in expected_values.items():
            if payload.get(key) != expected:
                errors.append(
                    _error(
                        "outbox_payload_mismatch",
                        stream,
                        f"outbox {row.id} {key}={payload.get(key)!r}, expected {expected!r}",
                    )
                )

    cycles = (await session.scalars(select(DecisionCycleModel))).all()
    for cycle in cycles:
        agent_count = int(
            await session.scalar(
                select(func.count())
                .select_from(AgentEvaluationModel)
                .where(AgentEvaluationModel.decision_cycle_id == cycle.id)
            )
            or 0
        )
        gate_count = int(
            await session.scalar(
                select(func.count())
                .select_from(GateEvaluationModel)
                .where(GateEvaluationModel.decision_cycle_id == cycle.id)
            )
            or 0
        )
        proposal_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TradeProposalModel)
                .where(TradeProposalModel.decision_cycle_id == cycle.id)
            )
            or 0
        )
        expected_event_count = 1 + agent_count + gate_count + proposal_count
        actual_event_count = int(
            await session.scalar(
                select(func.count())
                .select_from(DomainEventModel)
                .where(
                    DomainEventModel.aggregate_type == "decision_cycle",
                    DomainEventModel.aggregate_id == cycle.id,
                )
            )
            or 0
        )
        if agent_count != 8 or actual_event_count != expected_event_count:
            stream = next(
                (
                    item
                    for item in streams
                    if item.aggregate_type == "decision_cycle" and item.aggregate_id == cycle.id
                ),
                None,
            )
            errors.append(
                _error(
                    "decision_projection_mismatch",
                    stream,
                    f"agents={agent_count}, events={actual_event_count}, "
                    f"expected_events={expected_event_count}",
                )
            )

    experiments = (await session.scalars(select(ExperimentModel))).all()
    for experiment in experiments:
        try:
            rebuilt = await rebuild_experiment_projection(session, experiment.id)
        except ValueError as exc:
            stream = next(
                (
                    item
                    for item in streams
                    if item.aggregate_type == "experiment" and item.aggregate_id == experiment.id
                ),
                None,
            )
            errors.append(_error("experiment_rebuild_failed", stream, str(exc)))
            continue
        stored = RebuiltExperimentProjection(
            experiment_id=experiment.id,
            status=ExperimentStatus(experiment.status),
            started_at=experiment.started_at,
            ended_at=experiment.ended_at,
            failure_reason=experiment.failure_reason,
            config_hash=experiment.config_hash,
            manifest_hash=experiment.manifest_hash,
        )
        if rebuilt.normalized() != stored.normalized():
            stream = next(
                (
                    item
                    for item in streams
                    if item.aggregate_type == "experiment" and item.aggregate_id == experiment.id
                ),
                None,
            )
            errors.append(
                _error(
                    "experiment_projection_mismatch",
                    stream,
                    f"rebuilt={rebuilt.normalized()!r}, stored={stored.normalized()!r}",
                )
            )

    orders = (await session.scalars(select(OrderIntentModel))).all()
    for order in orders:
        projection_event_count = int(
            await session.scalar(
                select(func.count())
                .select_from(OrderEventModel)
                .where(OrderEventModel.order_intent_id == order.id)
            )
            or 0
        )
        domain_event_count = int(
            await session.scalar(
                select(func.count())
                .select_from(DomainEventModel)
                .where(
                    DomainEventModel.aggregate_type == "paper_order",
                    DomainEventModel.aggregate_id == order.id,
                )
            )
            or 0
        )
        filled_quantity = await session.scalar(
            select(func.coalesce(func.sum(FillModel.quantity), 0)).where(
                FillModel.order_intent_id == order.id
            )
        )
        if (
            projection_event_count != order.version
            or domain_event_count != order.version
            or filled_quantity != order.filled_quantity
        ):
            stream = next(
                (
                    item
                    for item in streams
                    if item.aggregate_type == "paper_order" and item.aggregate_id == order.id
                ),
                None,
            )
            errors.append(
                _error(
                    "order_projection_mismatch",
                    stream,
                    f"projection_events={projection_event_count}, "
                    f"domain_events={domain_event_count}, version={order.version}, "
                    f"fill_quantity={filled_quantity}, stored_fill={order.filled_quantity}",
                )
            )

    for experiment in experiments:
        latest_account = await session.scalar(
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.experiment_id == experiment.id)
            .order_by(AccountSnapshotModel.account_version.desc())
            .limit(1)
        )
        if latest_account is None:
            continue
        positions = (
            await session.scalars(
                select(PositionModel).where(PositionModel.experiment_id == experiment.id)
            )
        ).all()
        zero = Decimal("0")
        realized = sum((item.realized_pnl for item in positions), start=zero)
        unrealized = sum((item.unrealized_pnl for item in positions), start=zero)
        fees = sum((item.fees for item in positions), start=zero)
        funding = sum((item.funding for item in positions), start=zero)
        gross_notional = sum((item.quantity * item.mark_price for item in positions), start=zero)
        used_margin = sum((item.initial_margin for item in positions), start=zero)
        expected_cash = experiment.initial_capital + realized - fees + funding
        expected_equity = latest_account.cash_balance + unrealized
        expected_free_margin = expected_equity - used_margin
        active_exits = (
            await session.scalars(
                select(ExitPlanModel)
                .join(PositionModel, PositionModel.id == ExitPlanModel.position_id)
                .where(
                    PositionModel.experiment_id == experiment.id,
                    ExitPlanModel.status == "active",
                )
            )
        ).all()
        expected_risk_at_stop = sum(
            (
                abs(exit_plan.average_entry - exit_plan.stop_price) * exit_plan.quantity
                for exit_plan in active_exits
            ),
            start=zero,
        )
        if (
            latest_account.cash_balance != expected_cash
            or latest_account.equity != expected_equity
            or latest_account.free_margin != expected_free_margin
            or latest_account.realized_pnl != realized
            or latest_account.unrealized_pnl != unrealized
            or latest_account.fees != fees
            or latest_account.funding != funding
            or latest_account.gross_notional != gross_notional
            or latest_account.used_margin != used_margin
            or latest_account.risk_at_stop != expected_risk_at_stop
        ):
            stream = next(
                (
                    item
                    for item in streams
                    if item.aggregate_type == "paper_account" and item.aggregate_id == experiment.id
                ),
                None,
            )
            errors.append(
                _error(
                    "account_projection_mismatch",
                    stream,
                    f"stored_cash={latest_account.cash_balance}, expected_cash={expected_cash}, "
                    f"stored_equity={latest_account.equity}, expected_equity={expected_equity}",
                )
            )

    counterfactuals = (await session.scalars(select(CounterfactualModel))).all()
    for counterfactual in counterfactuals:
        event_count = int(
            await session.scalar(
                select(func.count())
                .select_from(DomainEventModel)
                .where(
                    DomainEventModel.aggregate_type == "counterfactual",
                    DomainEventModel.aggregate_id == counterfactual.id,
                )
            )
            or 0
        )
        hash_matches = content_hash(counterfactual.state_json) == counterfactual.content_hash
        if event_count != counterfactual.version or not hash_matches:
            stream = next(
                (
                    item
                    for item in streams
                    if item.aggregate_type == "counterfactual"
                    and item.aggregate_id == counterfactual.id
                ),
                None,
            )
            errors.append(
                _error(
                    "counterfactual_projection_mismatch",
                    stream,
                    f"events={event_count}, version={counterfactual.version}, "
                    f"hash_matches={hash_matches}",
                )
            )
    return LedgerConsistencyReport(tuple(errors))
