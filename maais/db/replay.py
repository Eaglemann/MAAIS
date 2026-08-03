from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
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
from maais.db.models.operations import (
    DataQualityEvaluationModel,
    IncidentModel,
    MarketCursorModel,
    MarketRecoveryRunModel,
    OperatorCommandModel,
    WorkerCheckpointModel,
)
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
    return _rebuild_experiment_projection_from_events(experiment_id, events)


def _rebuild_experiment_projection_from_events(
    experiment_id: UUID,
    events: Iterable[DomainEventModel],
) -> RebuiltExperimentProjection:
    ordered_events = sorted(events, key=lambda event: event.stream_version)
    if not ordered_events or ordered_events[0].event_type != "experiment.created":
        raise ValueError("experiment stream must begin with experiment.created")
    created = ordered_events[0]
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
    for event in ordered_events[1:]:
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


def _index_event_streams(
    streams: Iterable[EventStreamModel],
) -> tuple[
    dict[UUID, EventStreamModel],
    dict[tuple[str, UUID], EventStreamModel],
]:
    by_id: dict[UUID, EventStreamModel] = {}
    by_aggregate: dict[tuple[str, UUID], EventStreamModel] = {}
    for stream in streams:
        by_id[stream.id] = stream
        by_aggregate[(stream.aggregate_type, stream.aggregate_id)] = stream
    return by_id, by_aggregate


async def verify_ledger_consistency(session: AsyncSession) -> LedgerConsistencyReport:
    errors: list[LedgerConsistencyError] = []
    streams = (await session.scalars(select(EventStreamModel))).all()
    streams_by_id, streams_by_aggregate = _index_event_streams(streams)
    aggregate_event_counts = {
        (aggregate_type, aggregate_id): int(count)
        for aggregate_type, aggregate_id, count in (
            await session.execute(
                select(
                    DomainEventModel.aggregate_type,
                    DomainEventModel.aggregate_id,
                    func.count(DomainEventModel.id),
                ).group_by(
                    DomainEventModel.aggregate_type,
                    DomainEventModel.aggregate_id,
                )
            )
        ).all()
    }
    stream_stats = {
        stream_id: (
            int(event_count),
            minimum_version,
            maximum_version,
            int(distinct_versions),
            int(identity_mismatches),
        )
        for (
            stream_id,
            event_count,
            minimum_version,
            maximum_version,
            distinct_versions,
            identity_mismatches,
        ) in (
            await session.execute(
                select(
                    EventStreamModel.id,
                    func.count(DomainEventModel.id),
                    func.min(DomainEventModel.stream_version),
                    func.max(DomainEventModel.stream_version),
                    func.count(func.distinct(DomainEventModel.stream_version)),
                    func.count(DomainEventModel.id).filter(
                        or_(
                            DomainEventModel.aggregate_id.is_distinct_from(
                                EventStreamModel.aggregate_id
                            ),
                            DomainEventModel.aggregate_type.is_distinct_from(
                                EventStreamModel.aggregate_type
                            ),
                        )
                    ),
                )
                .outerjoin(
                    DomainEventModel,
                    DomainEventModel.stream_id == EventStreamModel.id,
                )
                .group_by(EventStreamModel.id)
            )
        ).all()
    }
    for stream in streams:
        (
            event_count,
            minimum_version,
            maximum_version,
            distinct_versions,
            identity_mismatches,
        ) = stream_stats.get(
            stream.id,
            (0, None, None, 0, 0),
        )
        versions_are_complete = (
            event_count == 0
            and distinct_versions == 0
            and minimum_version is None
            and maximum_version is None
            and stream.current_version == 0
        ) or (
            event_count == stream.current_version
            and distinct_versions == stream.current_version
            and minimum_version == 1
            and maximum_version == stream.current_version
        )
        if not versions_are_complete:
            errors.append(
                _error(
                    "stream_gap",
                    stream,
                    f"expected contiguous versions 1..{stream.current_version}, "
                    f"found count={event_count}, distinct={distinct_versions}, "
                    f"minimum={minimum_version}, maximum={maximum_version}",
                )
            )
        if identity_mismatches:
            errors.append(
                _error(
                    "stream_identity_mismatch",
                    stream,
                    f"{identity_mismatches} event identities differ from the stream",
                )
            )

    outbox_mismatches = (
        await session.execute(
            select(DomainEventModel, OutboxEventModel)
            .outerjoin(
                OutboxEventModel,
                OutboxEventModel.domain_event_id == DomainEventModel.id,
            )
            .where(
                or_(
                    OutboxEventModel.id.is_(None),
                    OutboxEventModel.payload_json["event_id"].is_distinct_from(
                        func.to_jsonb(DomainEventModel.id)
                    ),
                    OutboxEventModel.payload_json["global_position"].is_distinct_from(
                        func.to_jsonb(DomainEventModel.global_position)
                    ),
                    OutboxEventModel.payload_json["stream_version"].is_distinct_from(
                        func.to_jsonb(DomainEventModel.stream_version)
                    ),
                    OutboxEventModel.payload_json["event_type"].is_distinct_from(
                        func.to_jsonb(DomainEventModel.event_type)
                    ),
                )
            )
            .order_by(DomainEventModel.global_position)
        )
    ).all()
    for event, row in outbox_mismatches:
        stream = streams_by_id.get(event.stream_id)
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

    experiment_events_by_id: dict[UUID, list[DomainEventModel]] = {}
    experiment_events = (
        await session.scalars(
            select(DomainEventModel)
            .where(DomainEventModel.aggregate_type == "experiment")
            .order_by(DomainEventModel.global_position)
        )
    ).all()
    for event in experiment_events:
        experiment_events_by_id.setdefault(event.aggregate_id, []).append(event)

    agent_counts = {
        cycle_id: int(count)
        for cycle_id, count in (
            await session.execute(
                select(
                    AgentEvaluationModel.decision_cycle_id,
                    func.count(AgentEvaluationModel.id),
                ).group_by(AgentEvaluationModel.decision_cycle_id)
            )
        ).all()
    }
    gate_counts = {
        cycle_id: int(count)
        for cycle_id, count in (
            await session.execute(
                select(
                    GateEvaluationModel.decision_cycle_id,
                    func.count(GateEvaluationModel.id),
                ).group_by(GateEvaluationModel.decision_cycle_id)
            )
        ).all()
    }
    proposal_counts = {
        cycle_id: int(count)
        for cycle_id, count in (
            await session.execute(
                select(
                    TradeProposalModel.decision_cycle_id,
                    func.count(TradeProposalModel.id),
                ).group_by(TradeProposalModel.decision_cycle_id)
            )
        ).all()
    }
    cycles = (await session.scalars(select(DecisionCycleModel))).all()
    for cycle in cycles:
        agent_count = agent_counts.get(cycle.id, 0)
        gate_count = gate_counts.get(cycle.id, 0)
        proposal_count = proposal_counts.get(cycle.id, 0)
        expected_event_count = 1 + agent_count + gate_count + proposal_count
        actual_event_count = aggregate_event_counts.get(("decision_cycle", cycle.id), 0)
        if agent_count != 8 or actual_event_count != expected_event_count:
            stream = streams_by_aggregate.get(("decision_cycle", cycle.id))
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
        stream = streams_by_aggregate.get(("experiment", experiment.id))
        try:
            rebuilt = _rebuild_experiment_projection_from_events(
                experiment.id,
                experiment_events_by_id.get(experiment.id, []),
            )
        except ValueError as exc:
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
            stream = streams_by_aggregate.get(("experiment", experiment.id))
            errors.append(
                _error(
                    "experiment_projection_mismatch",
                    stream,
                    f"rebuilt={rebuilt.normalized()!r}, stored={stored.normalized()!r}",
                )
            )

    order_event_counts = (
        select(
            OrderEventModel.order_intent_id.label("order_intent_id"),
            func.count(OrderEventModel.id).label("projection_event_count"),
        )
        .group_by(OrderEventModel.order_intent_id)
        .subquery()
    )
    fill_quantities = (
        select(
            FillModel.order_intent_id.label("order_intent_id"),
            func.sum(FillModel.quantity).label("filled_quantity"),
        )
        .group_by(FillModel.order_intent_id)
        .subquery()
    )
    order_rows = (
        await session.execute(
            select(
                OrderIntentModel,
                func.coalesce(order_event_counts.c.projection_event_count, 0),
                func.coalesce(fill_quantities.c.filled_quantity, Decimal("0")),
            )
            .outerjoin(
                order_event_counts,
                order_event_counts.c.order_intent_id == OrderIntentModel.id,
            )
            .outerjoin(
                fill_quantities,
                fill_quantities.c.order_intent_id == OrderIntentModel.id,
            )
        )
    ).all()
    for order, raw_projection_event_count, filled_quantity in order_rows:
        projection_event_count = int(raw_projection_event_count)
        domain_event_count = aggregate_event_counts.get(("paper_order", order.id), 0)
        if (
            projection_event_count != order.version
            or domain_event_count != order.version
            or filled_quantity != order.filled_quantity
        ):
            stream = streams_by_aggregate.get(("paper_order", order.id))
            errors.append(
                _error(
                    "order_projection_mismatch",
                    stream,
                    f"projection_events={projection_event_count}, "
                    f"domain_events={domain_event_count}, version={order.version}, "
                    f"fill_quantity={filled_quantity}, stored_fill={order.filled_quantity}",
                )
            )

    account_snapshots = (
        await session.scalars(
            select(AccountSnapshotModel).order_by(
                AccountSnapshotModel.experiment_id,
                AccountSnapshotModel.account_version,
            )
        )
    ).all()
    latest_accounts: dict[UUID, AccountSnapshotModel] = {}
    for snapshot in account_snapshots:
        latest_accounts[snapshot.experiment_id] = snapshot
    positions_by_experiment: dict[UUID, list[PositionModel]] = {}
    active_exits_by_experiment: dict[UUID, list[ExitPlanModel]] = {}
    if account_snapshots:
        for position in (await session.scalars(select(PositionModel))).all():
            positions_by_experiment.setdefault(position.experiment_id, []).append(position)
        active_exit_rows = (
            await session.execute(
                select(PositionModel.experiment_id, ExitPlanModel)
                .join(ExitPlanModel, ExitPlanModel.position_id == PositionModel.id)
                .where(ExitPlanModel.status.in_(("active", "triggered")))
            )
        ).all()
        for experiment_id, exit_plan in active_exit_rows:
            active_exits_by_experiment.setdefault(experiment_id, []).append(exit_plan)

    for experiment in experiments:
        latest_account = latest_accounts.get(experiment.id)
        if latest_account is None:
            continue
        positions = positions_by_experiment.get(experiment.id, [])
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
        active_exits = active_exits_by_experiment.get(experiment.id, [])
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
            stream = streams_by_aggregate.get(("paper_account", experiment.id))
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
        event_count = aggregate_event_counts.get(("counterfactual", counterfactual.id), 0)
        hash_matches = content_hash(counterfactual.state_json) == counterfactual.content_hash
        if event_count != counterfactual.version or not hash_matches:
            stream = streams_by_aggregate.get(("counterfactual", counterfactual.id))
            errors.append(
                _error(
                    "counterfactual_projection_mismatch",
                    stream,
                    f"events={event_count}, version={counterfactual.version}, "
                    f"hash_matches={hash_matches}",
                )
            )

    versioned_operational_rows: tuple[
        tuple[str, list[MarketCursorModel | MarketRecoveryRunModel | IncidentModel]], ...
    ] = (
        ("market_cursor", list((await session.scalars(select(MarketCursorModel))).all())),
        (
            "market_recovery",
            list((await session.scalars(select(MarketRecoveryRunModel))).all()),
        ),
        ("incident", list((await session.scalars(select(IncidentModel))).all())),
    )
    for aggregate_type, rows in versioned_operational_rows:
        for row in rows:
            event_count = aggregate_event_counts.get((aggregate_type, row.id), 0)
            hash_matches = content_hash(row.state_json) == row.content_hash
            if event_count != row.version or not hash_matches:
                stream = streams_by_aggregate.get((aggregate_type, row.id))
                errors.append(
                    _error(
                        f"{aggregate_type}_projection_mismatch",
                        stream,
                        f"events={event_count}, version={row.version}, hash_matches={hash_matches}",
                    )
                )

    checkpoints = (await session.scalars(select(WorkerCheckpointModel))).all()
    for checkpoint in checkpoints:
        event_count = aggregate_event_counts.get(("worker_checkpoint", checkpoint.experiment_id), 0)
        hash_matches = content_hash(checkpoint.state_json) == checkpoint.content_hash
        if event_count != checkpoint.version or not hash_matches:
            stream = streams_by_aggregate.get(("worker_checkpoint", checkpoint.experiment_id))
            errors.append(
                _error(
                    "worker_checkpoint_projection_mismatch",
                    stream,
                    f"events={event_count}, version={checkpoint.version}, "
                    f"hash_matches={hash_matches}",
                )
            )

    quality_rows = (await session.scalars(select(DataQualityEvaluationModel))).all()
    quality_by_frame: dict[UUID, list[DataQualityEvaluationModel]] = {}
    for row in quality_rows:
        quality_by_frame.setdefault(row.market_frame_id, []).append(row)
        expected_hash = content_hash(
            {
                "market_frame_id": row.market_frame_id,
                "check_name": row.check_name,
                "required": row.required,
                "status": row.status,
                "reason_code": row.reason_code,
                "details": row.details_json,
                "evaluated_at": row.evaluated_at,
            }
        )
        if expected_hash != row.content_hash:
            errors.append(
                LedgerConsistencyError(
                    code="market_quality_row_hash_mismatch",
                    aggregate_type="market_quality",
                    aggregate_id=row.market_frame_id,
                    details=f"quality row {row.id} content hash differs",
                )
            )
    for frame_id, rows in quality_by_frame.items():
        names = {row.check_name for row in rows}
        event_count = aggregate_event_counts.get(("market_quality", frame_id), 0)
        if len(names) != len(rows) or event_count != 1:
            stream = streams_by_aggregate.get(("market_quality", frame_id))
            errors.append(
                _error(
                    "market_quality_projection_mismatch",
                    stream,
                    f"rows={len(rows)}, unique_checks={len(names)}, events={event_count}",
                )
            )

    operator_commands = (await session.scalars(select(OperatorCommandModel))).all()
    for command in operator_commands:
        expected_hash = content_hash(
            {
                "command_id": command.id,
                "experiment_id": command.experiment_id,
                "command_type": command.command_type,
                "status": command.status,
                "idempotency_key": command.idempotency_key,
                "actor": command.actor,
                "reason": command.reason,
                "payload": command.payload_json,
                "operator_confirmed": command.operator_confirmed,
                "request_hash": command.request_hash,
                "requested_at": command.requested_at,
                "version": command.version,
                "accepted_at": command.accepted_at,
                "accepted_by": command.accepted_by,
                "completed_at": command.completed_at,
                "result": command.result_json,
            }
        )
        event_count = aggregate_event_counts.get(("operator_command", command.id), 0)
        if expected_hash != command.content_hash or event_count != command.version:
            stream = streams_by_aggregate.get(("operator_command", command.id))
            errors.append(
                _error(
                    "operator_command_projection_mismatch",
                    stream,
                    f"events={event_count}, version={command.version}, "
                    f"hash_matches={expected_hash == command.content_hash}",
                )
            )
    return LedgerConsistencyReport(tuple(errors))
