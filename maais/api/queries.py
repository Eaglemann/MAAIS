from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from maais.api.schemas import (
    AccountOverview,
    AuditEvent,
    DataFreshness,
    DecisionCounts,
    DecisionDetail,
    DecisionListItem,
    DecisionPage,
    ExperimentIdentity,
    ExperimentListItem,
    ExperimentOverview,
    OperationalCounts,
    RuntimeOverview,
)
from maais.db.models.accounts import AccountSnapshotModel, PositionModel
from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    DecisionSummaryModel,
    GateEvaluationModel,
    MarketFrameModel,
    TradeProposalModel,
)
from maais.db.models.execution import FillModel, OrderEventModel, OrderIntentModel
from maais.db.models.experiments import AgentVersionModel, ExperimentModel
from maais.db.models.ledger import DomainEventModel
from maais.db.models.operations import (
    DataQualityEvaluationModel,
    IncidentModel,
    MarketCursorModel,
    MarketRecoveryRunModel,
    TradingControlModel,
    WorkerCheckpointModel,
    WorkerLeaseModel,
)

_PENDING_ORDER_STATUSES = ("created", "authorized", "accepted", "partially_filled")
_OPEN_COUNTERFACTUAL_STATUSES = ("pending", "open")
_ACTIVE_RECOVERY_STATUSES = ("detected", "backfilling")


def _fields(model: object, names: Iterable[str]) -> dict[str, object]:
    return {name: getattr(model, name) for name in names}


class MissionControlQueryService:
    """Read-only query model over authoritative PostgreSQL projections."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_experiments(self, *, limit: int = 50) -> tuple[ExperimentListItem, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("experiment limit must be between 1 and 200")
        experiments = (
            await self._session.scalars(
                select(ExperimentModel)
                .order_by(ExperimentModel.created_at.desc(), ExperimentModel.id)
                .limit(limit)
            )
        ).all()
        items: list[ExperimentListItem] = []
        for experiment in experiments:
            items.append(await self._experiment_item(experiment))
        return tuple(items)

    async def get_overview(self, experiment_id: UUID) -> ExperimentOverview:
        experiment = await self._session.get(ExperimentModel, experiment_id)
        if experiment is None:
            raise LookupError(f"experiment not found: {experiment_id}")
        item = await self._experiment_item(experiment)
        positions = (
            await self._session.scalars(
                select(PositionModel)
                .where(
                    PositionModel.experiment_id == experiment_id,
                    PositionModel.status == "open",
                )
                .order_by(PositionModel.symbol)
            )
        ).all()
        pending_orders = (
            await self._session.scalars(
                select(OrderIntentModel)
                .where(
                    OrderIntentModel.experiment_id == experiment_id,
                    OrderIntentModel.status.in_(_PENDING_ORDER_STATUSES),
                )
                .order_by(OrderIntentModel.created_at, OrderIntentModel.id)
            )
        ).all()
        incidents = (
            await self._session.scalars(
                select(IncidentModel)
                .where(
                    IncidentModel.experiment_id == experiment_id,
                    IncidentModel.status != "resolved",
                )
                .order_by(IncidentModel.detected_at.desc(), IncidentModel.id)
            )
        ).all()
        return ExperimentOverview(
            **item.model_dump(),
            positions=tuple(self._position(position) for position in positions),
            pending_orders=tuple(self._order(order) for order in pending_orders),
            incidents=tuple(self._incident(incident) for incident in incidents),
        )

    async def list_decisions(
        self,
        experiment_id: UUID,
        *,
        symbol: str | None = None,
        status: str | None = None,
        disposition: str | None = None,
        reason_code: str | None = None,
        before: datetime | None = None,
        limit: int = 100,
    ) -> DecisionPage:
        if not 1 <= limit <= 500:
            raise ValueError("decision limit must be between 1 and 500")
        if symbol is not None:
            symbol = symbol.upper()
        statement = (
            select(
                DecisionCycleModel,
                MarketFrameModel.quality_status,
                DecisionSummaryModel.consensus_direction,
                DecisionSummaryModel.consensus_probability,
                DecisionSummaryModel.consensus_confidence,
                TradeProposalModel.status.label("proposal_status"),
                OrderIntentModel.status.label("order_status"),
                CounterfactualModel.status.label("counterfactual_status"),
            )
            .join(MarketFrameModel, MarketFrameModel.id == DecisionCycleModel.market_frame_id)
            .outerjoin(
                DecisionSummaryModel,
                DecisionSummaryModel.decision_cycle_id == DecisionCycleModel.id,
            )
            .outerjoin(
                TradeProposalModel,
                TradeProposalModel.decision_cycle_id == DecisionCycleModel.id,
            )
            .outerjoin(OrderIntentModel, OrderIntentModel.proposal_id == TradeProposalModel.id)
            .outerjoin(
                CounterfactualModel,
                CounterfactualModel.decision_cycle_id == DecisionCycleModel.id,
            )
            .where(DecisionCycleModel.experiment_id == experiment_id)
        )
        if symbol is not None:
            statement = statement.where(DecisionCycleModel.symbol == symbol)
        if status is not None:
            statement = statement.where(DecisionCycleModel.status == status)
        if disposition is not None:
            statement = statement.where(DecisionCycleModel.disposition == disposition)
        if reason_code is not None:
            statement = statement.where(DecisionCycleModel.reason_code == reason_code)
        if before is not None:
            statement = statement.where(DecisionCycleModel.cycle_at < before)
        rows = (
            await self._session.execute(
                statement.order_by(
                    DecisionCycleModel.cycle_at.desc(),
                    DecisionCycleModel.symbol,
                    DecisionCycleModel.id,
                ).limit(limit + 1)
            )
        ).all()
        has_more = len(rows) > limit
        items = tuple(self._decision_item(*row) for row in rows[:limit])
        return DecisionPage(
            items=items,
            limit=limit,
            has_more=has_more,
            next_before=items[-1].cycle_at if has_more and items else None,
        )

    async def get_decision(self, decision_id: UUID) -> DecisionDetail:
        cycle = await self._session.get(DecisionCycleModel, decision_id)
        if cycle is None:
            raise LookupError(f"decision not found: {decision_id}")
        frame = await self._session.get(MarketFrameModel, cycle.market_frame_id)
        if frame is None:
            raise RuntimeError("decision market frame is missing")
        summary = await self._session.get(DecisionSummaryModel, decision_id)
        proposal = await self._session.scalar(
            select(TradeProposalModel).where(TradeProposalModel.decision_cycle_id == decision_id)
        )
        agents = (
            await self._session.execute(
                select(AgentEvaluationModel, AgentVersionModel)
                .join(
                    AgentVersionModel, AgentVersionModel.id == AgentEvaluationModel.agent_version_id
                )
                .where(AgentEvaluationModel.decision_cycle_id == decision_id)
                .order_by(AgentVersionModel.agent_name)
            )
        ).all()
        gates = (
            await self._session.scalars(
                select(GateEvaluationModel)
                .where(GateEvaluationModel.decision_cycle_id == decision_id)
                .order_by(GateEvaluationModel.sequence)
            )
        ).all()
        quality = (
            await self._session.scalars(
                select(DataQualityEvaluationModel)
                .where(DataQualityEvaluationModel.market_frame_id == frame.id)
                .order_by(DataQualityEvaluationModel.check_name)
            )
        ).all()
        orders = []
        if proposal is not None:
            order_models = (
                await self._session.scalars(
                    select(OrderIntentModel)
                    .where(OrderIntentModel.proposal_id == proposal.id)
                    .order_by(OrderIntentModel.created_at, OrderIntentModel.id)
                )
            ).all()
            for order in order_models:
                events = (
                    await self._session.scalars(
                        select(OrderEventModel)
                        .where(OrderEventModel.order_intent_id == order.id)
                        .order_by(OrderEventModel.sequence)
                    )
                ).all()
                fills = (
                    await self._session.scalars(
                        select(FillModel)
                        .where(FillModel.order_intent_id == order.id)
                        .order_by(FillModel.fill_at, FillModel.id)
                    )
                ).all()
                orders.append(
                    {
                        **self._order(order),
                        "events": [self._order_event(event) for event in events],
                        "fills": [self._fill(fill) for fill in fills],
                    }
                )
        counterfactual = await self._session.scalar(
            select(CounterfactualModel).where(CounterfactualModel.decision_cycle_id == decision_id)
        )
        incident = await self._session.scalar(
            select(IncidentModel).where(
                IncidentModel.experiment_id == cycle.experiment_id,
                IncidentModel.evidence_json["frame_id"].as_string() == str(frame.id),
            )
        )
        aggregate_ids = {decision_id}
        if proposal is not None:
            aggregate_ids.add(proposal.id)
        if counterfactual is not None:
            aggregate_ids.add(counterfactual.id)
        if incident is not None:
            aggregate_ids.add(incident.id)
        aggregate_ids.update(UUID(str(order["id"])) for order in orders)
        events = (
            await self._session.scalars(
                select(DomainEventModel)
                .where(DomainEventModel.aggregate_id.in_(aggregate_ids))
                .order_by(DomainEventModel.global_position)
            )
        ).all()
        item = await self._decision_item_for_cycle(cycle, frame, summary, proposal, counterfactual)
        return DecisionDetail(
            decision=item,
            cycle=self._cycle(cycle),
            market_frame=self._frame(frame),
            quality_evaluations=tuple(self._quality(row) for row in quality),
            agents=tuple(self._agent(evaluation, version) for evaluation, version in agents),
            summary=self._summary(summary) if summary is not None else None,
            gates=tuple(self._gate(gate) for gate in gates),
            proposal=self._proposal(proposal) if proposal is not None else None,
            orders=tuple(orders),
            counterfactual=(
                self._counterfactual(counterfactual) if counterfactual is not None else None
            ),
            incident=self._incident(incident) if incident is not None else None,
            timeline=tuple(self._audit_event(event) for event in events),
            lineage_hashes={
                "experiment_manifest": (await self._experiment(cycle.experiment_id)).manifest_hash,
                "market_frame": frame.content_hash,
                "decision_cycle": cycle.content_hash,
            },
        )

    async def _experiment_item(self, experiment: ExperimentModel) -> ExperimentListItem:
        experiment_id = experiment.id
        account = await self._latest_account(experiment)
        checkpoint = await self._session.get(WorkerCheckpointModel, experiment_id)
        lease = await self._session.get(WorkerLeaseModel, experiment_id)
        control = await self._session.get(TradingControlModel, experiment_id)
        grouped_decisions = (
            await self._session.execute(
                select(
                    DecisionCycleModel.status,
                    DecisionCycleModel.disposition,
                    func.count(),
                )
                .where(DecisionCycleModel.experiment_id == experiment_id)
                .group_by(DecisionCycleModel.status, DecisionCycleModel.disposition)
            )
        ).all()
        decisions = DecisionCounts(
            total=sum(count for _, _, count in grouped_decisions),
            completed=sum(count for status, _, count in grouped_decisions if status == "completed"),
            rejected=sum(count for status, _, count in grouped_decisions if status == "rejected"),
            quarantined=sum(
                count for status, _, count in grouped_decisions if status == "quarantined"
            ),
            neutral=sum(
                count for _, disposition, count in grouped_decisions if disposition == "neutral"
            ),
            approved=sum(
                count for _, disposition, count in grouped_decisions if disposition == "approved"
            ),
            directional_rejected=sum(
                count for _, disposition, count in grouped_decisions if disposition == "rejected"
            ),
        )
        (
            open_positions,
            pending_orders,
            fills,
            open_incidents,
            review_incidents,
            open_cfs,
        ) = await self._operation_counts(experiment_id)
        cursor_count, latest_bar, latest_update, halted_cursors = (
            await self._session.execute(
                select(
                    func.count(MarketCursorModel.id),
                    func.max(MarketCursorModel.bar_close_at),
                    func.max(MarketCursorModel.updated_at),
                    func.count(MarketCursorModel.id).filter(MarketCursorModel.status == "halted"),
                ).where(MarketCursorModel.experiment_id == experiment_id)
            )
        ).one()
        active_recoveries = int(
            await self._session.scalar(
                select(func.count())
                .select_from(MarketRecoveryRunModel)
                .where(
                    MarketRecoveryRunModel.experiment_id == experiment_id,
                    MarketRecoveryRunModel.status.in_(_ACTIVE_RECOVERY_STATUSES),
                )
            )
            or 0
        )
        symbols = experiment.manifest_json.get("symbols", [])
        expected_symbols = len(symbols) if isinstance(symbols, list) else 0
        return ExperimentListItem(
            experiment=self._identity(experiment),
            account=account,
            runtime=RuntimeOverview(
                worker_status=checkpoint.status if checkpoint is not None else None,
                checkpoint_at=checkpoint.checkpoint_at if checkpoint is not None else None,
                checkpoint_version=checkpoint.version if checkpoint is not None else None,
                lease_status=lease.status if lease is not None else None,
                lease_heartbeat_at=lease.heartbeat_at if lease is not None else None,
                lease_expires_at=lease.expires_at if lease is not None else None,
                lease_released_at=lease.released_at if lease is not None else None,
                lease_epoch=lease.epoch if lease is not None else None,
                kill_switch_active=control.kill_switch_active if control is not None else False,
                kill_switch_reason=control.reason if control is not None else None,
                control_version=control.version if control is not None else None,
            ),
            decisions=decisions,
            operations=OperationalCounts(
                open_positions=open_positions,
                pending_orders=pending_orders,
                fills=fills,
                open_incidents=open_incidents,
                review_incidents=review_incidents,
                pending_counterfactuals=open_cfs,
            ),
            freshness=DataFreshness(
                expected_symbols=expected_symbols,
                cursor_count=int(cursor_count or 0),
                latest_bar_close_at=latest_bar,
                latest_cursor_update_at=latest_update,
                halted_cursors=int(halted_cursors or 0),
                active_recoveries=active_recoveries,
            ),
        )

    async def _latest_account(self, experiment: ExperimentModel) -> AccountOverview:
        snapshot = await self._session.scalar(
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.experiment_id == experiment.id)
            .order_by(AccountSnapshotModel.account_version.desc())
            .limit(1)
        )
        if snapshot is None:
            zero = Decimal("0")
            return AccountOverview(
                source="manifest_initial_state",
                snapshot_at=None,
                account_version=0,
                cash_balance=experiment.initial_capital,
                equity=experiment.initial_capital,
                used_margin=zero,
                free_margin=experiment.initial_capital,
                gross_notional=zero,
                risk_at_stop=zero,
                unrealized_pnl=zero,
                realized_pnl=zero,
                fees=zero,
                funding=zero,
                peak_equity=experiment.initial_capital,
                drawdown=zero,
            )
        return AccountOverview.model_validate(
            {
                "source": "account_snapshot",
                **_fields(
                    snapshot,
                    (
                        "snapshot_at",
                        "account_version",
                        "cash_balance",
                        "equity",
                        "used_margin",
                        "free_margin",
                        "gross_notional",
                        "risk_at_stop",
                        "unrealized_pnl",
                        "realized_pnl",
                        "fees",
                        "funding",
                        "peak_equity",
                        "drawdown",
                    ),
                ),
            }
        )

    async def _operation_counts(self, experiment_id: UUID) -> tuple[int, ...]:
        statements = (
            select(func.count())
            .select_from(PositionModel)
            .where(PositionModel.experiment_id == experiment_id, PositionModel.status == "open"),
            select(func.count())
            .select_from(OrderIntentModel)
            .where(
                OrderIntentModel.experiment_id == experiment_id,
                OrderIntentModel.status.in_(_PENDING_ORDER_STATUSES),
            ),
            select(func.count())
            .select_from(FillModel)
            .join(OrderIntentModel, OrderIntentModel.id == FillModel.order_intent_id)
            .where(OrderIntentModel.experiment_id == experiment_id),
            select(func.count())
            .select_from(IncidentModel)
            .where(
                IncidentModel.experiment_id == experiment_id, IncidentModel.status != "resolved"
            ),
            select(func.count())
            .select_from(IncidentModel)
            .where(
                IncidentModel.experiment_id == experiment_id,
                IncidentModel.status != "resolved",
                IncidentModel.requires_operator_review.is_(True),
            ),
            select(func.count())
            .select_from(CounterfactualModel)
            .where(
                CounterfactualModel.experiment_id == experiment_id,
                CounterfactualModel.status.in_(_OPEN_COUNTERFACTUAL_STATUSES),
            ),
        )
        counts: list[int] = []
        for statement in statements:
            counts.append(int(await self._session.scalar(statement) or 0))
        return tuple(counts)

    async def _decision_item_for_cycle(
        self,
        cycle: DecisionCycleModel,
        frame: MarketFrameModel,
        summary: DecisionSummaryModel | None,
        proposal: TradeProposalModel | None,
        counterfactual: CounterfactualModel | None,
    ) -> DecisionListItem:
        order_status = None
        if proposal is not None:
            order_status = await self._session.scalar(
                select(OrderIntentModel.status)
                .where(OrderIntentModel.proposal_id == proposal.id)
                .order_by(OrderIntentModel.created_at.desc())
                .limit(1)
            )
        return self._decision_item(
            cycle,
            frame.quality_status,
            summary.consensus_direction if summary is not None else None,
            summary.consensus_probability if summary is not None else None,
            summary.consensus_confidence if summary is not None else None,
            proposal.status if proposal is not None else None,
            order_status,
            counterfactual.status if counterfactual is not None else None,
        )

    @staticmethod
    def _decision_item(
        cycle: DecisionCycleModel,
        quality_status: str,
        consensus_direction: str | None,
        consensus_probability: Decimal | None,
        consensus_confidence: Decimal | None,
        proposal_status: str | None,
        order_status: str | None,
        counterfactual_status: str | None,
    ) -> DecisionListItem:
        return DecisionListItem.model_validate(
            {
                **_fields(
                    cycle,
                    (
                        "id",
                        "experiment_id",
                        "market_frame_id",
                        "symbol",
                        "timeframe",
                        "cycle_at",
                        "regime",
                        "status",
                        "direction",
                        "disposition",
                        "reason_code",
                        "created_at",
                        "completed_at",
                    ),
                ),
                "quality_status": quality_status,
                "consensus_direction": consensus_direction,
                "consensus_probability": consensus_probability,
                "consensus_confidence": consensus_confidence,
                "proposal_status": proposal_status,
                "order_status": order_status,
                "counterfactual_status": counterfactual_status,
            }
        )

    async def _experiment(self, experiment_id: UUID) -> ExperimentModel:
        experiment = await self._session.get(ExperimentModel, experiment_id)
        if experiment is None:
            raise RuntimeError("decision experiment is missing")
        return experiment

    @staticmethod
    def _identity(model: ExperimentModel) -> ExperimentIdentity:
        return ExperimentIdentity.model_validate(
            _fields(
                model,
                (
                    "id",
                    "name",
                    "mode",
                    "status",
                    "initial_capital",
                    "currency",
                    "created_at",
                    "started_at",
                    "ended_at",
                    "failure_reason",
                    "git_sha",
                    "worktree_hash",
                    "lock_hash",
                    "schema_revision",
                    "config_hash",
                    "manifest_hash",
                    "manifest_schema_version",
                ),
            )
        )

    @staticmethod
    def _position(model: PositionModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in PositionModel.__table__.columns))

    @staticmethod
    def _order(model: OrderIntentModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in OrderIntentModel.__table__.columns))

    @staticmethod
    def _incident(model: IncidentModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in IncidentModel.__table__.columns))

    @staticmethod
    def _cycle(model: DecisionCycleModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in DecisionCycleModel.__table__.columns))

    @staticmethod
    def _frame(model: MarketFrameModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in MarketFrameModel.__table__.columns))

    @staticmethod
    def _quality(model: DataQualityEvaluationModel) -> dict[str, object]:
        return _fields(
            model, tuple(column.name for column in DataQualityEvaluationModel.__table__.columns)
        )

    @staticmethod
    def _agent(model: AgentEvaluationModel, version: AgentVersionModel) -> dict[str, object]:
        return {
            **_fields(
                model, tuple(column.name for column in AgentEvaluationModel.__table__.columns)
            ),
            "agent_name": version.agent_name,
            "agent_version": version.version,
            "maturity": version.maturity,
            "implementation_hash": version.implementation_hash,
            "data_dependencies": version.data_dependencies_json,
        }

    @staticmethod
    def _summary(model: DecisionSummaryModel) -> dict[str, object]:
        return _fields(
            model, tuple(column.name for column in DecisionSummaryModel.__table__.columns)
        )

    @staticmethod
    def _gate(model: GateEvaluationModel) -> dict[str, object]:
        return _fields(
            model, tuple(column.name for column in GateEvaluationModel.__table__.columns)
        )

    @staticmethod
    def _proposal(model: TradeProposalModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in TradeProposalModel.__table__.columns))

    @staticmethod
    def _counterfactual(model: CounterfactualModel) -> dict[str, object]:
        return _fields(
            model, tuple(column.name for column in CounterfactualModel.__table__.columns)
        )

    @staticmethod
    def _order_event(model: OrderEventModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in OrderEventModel.__table__.columns))

    @staticmethod
    def _fill(model: FillModel) -> dict[str, object]:
        return _fields(model, tuple(column.name for column in FillModel.__table__.columns))

    @staticmethod
    def _audit_event(model: DomainEventModel) -> AuditEvent:
        return AuditEvent.model_validate(
            {
                **_fields(
                    model,
                    (
                        "id",
                        "global_position",
                        "aggregate_id",
                        "aggregate_type",
                        "stream_version",
                        "event_type",
                        "event_version",
                        "occurred_at",
                        "recorded_at",
                    ),
                ),
                "payload": model.payload_json,
                "metadata": model.metadata_json,
            }
        )
