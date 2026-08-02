from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.accounts import (
    AccountSnapshotModel,
    ExitPlanModel,
    FundingEntryModel,
    PositionLotModel,
    PositionModel,
)
from maais.db.models.execution import (
    ExecutionSensitivityModel,
    FillModel,
    OrderEventModel,
    OrderIntentModel,
)
from maais.db.models.experiments import ExperimentModel
from maais.db.repositories.events import EventRepository
from maais.domain.enums import (
    Direction,
    PaperOrderSide,
    PaperOrderStatus,
    PaperOrderType,
    PositionEffect,
)
from maais.domain.events import NewDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, content_hash, freeze_json, to_json_data
from maais.execution.paper.account import AccountState
from maais.execution.paper.exits import ExitPlan, ExitPlanStatus, ExitReason
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.orders import OrderTransition, PaperOrder
from maais.execution.paper.positions import PositionLot, PositionState
from maais.execution.paper.records import (
    ExecutionFillRecord,
    FundingRecord,
    PaperExecutionRecord,
)
from maais.execution.paper.sensitivity import SensitivityOutcome, SensitivityScenario


class OrderIdentityConflict(RuntimeError):
    pass


class StaleExecutionState(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    created: bool
    order_intent_id: UUID
    order_version: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class PendingPaperOrder:
    order: PaperOrder
    exchange_filters: ExchangeFilterSnapshot


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    result = to_json_data(value)
    if not isinstance(result, dict):
        raise TypeError("expected JSON object")
    return result


def _event_object(value: object) -> Mapping[str, JsonValue]:
    result = freeze_json(value)
    if not isinstance(result, Mapping):
        raise TypeError("expected event object")
    return result


def _filters_dict(filters: ExchangeFilterSnapshot) -> dict[str, object]:
    return {
        "symbol": filters.symbol,
        "status": filters.status,
        "price_tick": filters.price_tick,
        "quantity_step": filters.quantity_step,
        "minimum_quantity": filters.minimum_quantity,
        "maximum_quantity": filters.maximum_quantity,
        "minimum_notional": filters.minimum_notional,
        "supported_order_types": filters.supported_order_types,
        "captured_at": filters.captured_at,
    }


def _filters_from_json(value: Mapping[str, object]) -> ExchangeFilterSnapshot:
    captured_at = value.get("captured_at")
    supported_order_types = value.get("supported_order_types")
    if not isinstance(captured_at, str):
        raise TypeError("stored exchange-filter captured_at must be a string")
    if not isinstance(supported_order_types, list):
        raise TypeError("stored supported_order_types must be a list")
    return ExchangeFilterSnapshot(
        symbol=str(value["symbol"]),
        status=str(value["status"]),
        price_tick=Decimal(str(value["price_tick"])),
        quantity_step=Decimal(str(value["quantity_step"])),
        minimum_quantity=Decimal(str(value["minimum_quantity"])),
        maximum_quantity=Decimal(str(value["maximum_quantity"])),
        minimum_notional=Decimal(str(value["minimum_notional"])),
        supported_order_types=tuple(
            PaperOrderType(str(order_type)) for order_type in supported_order_types
        ),
        captured_at=datetime.fromisoformat(captured_at.replace("Z", "+00:00")),
    )


def _order_dict(order: PaperOrder) -> dict[str, object]:
    return {
        "order_id": order.order_id,
        "experiment_id": order.experiment_id,
        "proposal_id": order.proposal_id,
        "client_order_id": order.client_order_id,
        "command_hash": order.command_hash,
        "symbol": order.symbol,
        "side": order.side,
        "order_type": order.order_type,
        "position_effect": order.position_effect,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "limit_price": order.limit_price,
        "reduce_only": order.reduce_only,
        "status": order.status,
        "created_at": order.created_at,
        "expires_at": order.expires_at,
        "version": order.version,
        "events": [
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "event_at": event.event_at,
                "payload": event.payload,
            }
            for event in order.events
        ],
    }


def _execution_hash(record: PaperExecutionRecord) -> str:
    return content_hash(
        {
            "order": _order_dict(record.order),
            "exchange_filters": _filters_dict(record.exchange_filters),
            "fills": [fill.to_dict() for fill in record.fills],
        }
    )


def _snapshot_dict(account: AccountState) -> dict[str, object]:
    snapshot = account.snapshot()
    return {
        "cash_balance": snapshot.cash_balance,
        "equity": snapshot.equity,
        "used_margin": snapshot.used_margin,
        "free_margin": snapshot.free_margin,
        "gross_notional": snapshot.gross_notional,
        "unrealized_pnl": snapshot.unrealized_pnl,
        "realized_pnl": snapshot.realized_pnl,
        "fees": snapshot.fees,
        "funding": snapshot.funding,
        "peak_equity": snapshot.peak_equity,
        "drawdown": snapshot.drawdown,
    }


class PaperExecutionRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def record(self, record: PaperExecutionRecord) -> PaperExecutionResult:
        record.validate()
        order = record.order
        execution_hash = _execution_hash(record)
        inserted_id = await self._session.scalar(
            insert(OrderIntentModel)
            .values(**self._order_values(record, execution_hash))
            .on_conflict_do_nothing(
                index_elements=[
                    OrderIntentModel.experiment_id,
                    OrderIntentModel.client_order_id,
                ]
            )
            .returning(OrderIntentModel.id)
        )
        created = inserted_id is not None
        previous_version = 0
        if created:
            new_transitions = order.events
        else:
            existing = await self._session.scalar(
                select(OrderIntentModel)
                .where(
                    OrderIntentModel.experiment_id == order.experiment_id,
                    OrderIntentModel.client_order_id == order.client_order_id,
                )
                .with_for_update()
            )
            if existing is None:
                raise RuntimeError("order identity disappeared after conflict")
            if existing.command_hash != order.command_hash or existing.id != order.order_id:
                raise OrderIdentityConflict("client order identity has different command content")
            previous_version = existing.version
            if order.version < previous_version:
                raise StaleExecutionState("order state is older than the persisted version")
            if order.version == previous_version:
                if existing.content_hash != execution_hash:
                    raise OrderIdentityConflict("order version has different execution content")
                return PaperExecutionResult(False, existing.id, existing.version, execution_hash)
            new_transitions = tuple(
                transition for transition in order.events if transition.sequence > previous_version
            )
            if not new_transitions or new_transitions[0].sequence != previous_version + 1:
                raise StaleExecutionState("order transition sequence is not contiguous")
            for key, value in self._order_values(record, execution_hash).items():
                if key != "id":
                    setattr(existing, key, value)

        for transition in new_transitions:
            self._session.add(
                OrderEventModel(
                    id=uuid4(),
                    order_intent_id=order.order_id,
                    sequence=transition.sequence,
                    event_type=transition.event_type,
                    event_at=transition.event_at,
                    market_frame_id=None,
                    payload_json=_json_object(transition.payload),
                )
            )
        created_fills: list[ExecutionFillRecord] = []
        for fill in record.fills:
            fill_id = await self._session.scalar(
                insert(FillModel)
                .values(**self._fill_values(fill))
                .on_conflict_do_nothing(index_elements=[FillModel.id])
                .returning(FillModel.id)
            )
            if fill_id is not None:
                created_fills.append(fill)
            else:
                existing_fill = await self._session.get(FillModel, fill.id)
                if existing_fill is None or not self._same_fill(existing_fill, fill):
                    raise OrderIdentityConflict("fill identity has different content")
        await self._session.flush()
        account_snapshot_created = False
        if record.account is not None:
            account_snapshot_created = await self._persist_account(record.account, record.exit_plan)
        await self._append_events(
            record,
            previous_order_version=previous_version,
            new_transitions=new_transitions,
            created_fills=tuple(created_fills),
            account_snapshot_created=account_snapshot_created,
        )
        return PaperExecutionResult(created, order.order_id, order.version, execution_hash)

    async def load_account(self, experiment_id: UUID) -> AccountState:
        experiment = await self._session.get(ExperimentModel, experiment_id)
        if experiment is None:
            raise LookupError("experiment does not exist")
        snapshot = await self._session.scalar(
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.experiment_id == experiment_id)
            .order_by(AccountSnapshotModel.account_version.desc())
            .limit(1)
        )
        if snapshot is None:
            return AccountState.create(
                experiment_id,
                experiment.initial_capital,
                experiment.currency,
                leverage=1,
            )
        position_rows = (
            await self._session.scalars(
                select(PositionModel).where(PositionModel.experiment_id == experiment_id)
            )
        ).all()
        positions: dict[str, PositionState] = {}
        for row in position_rows:
            lot_rows = (
                await self._session.scalars(
                    select(PositionLotModel)
                    .where(PositionLotModel.position_id == row.id)
                    .order_by(PositionLotModel.opened_at, PositionLotModel.id)
                )
            ).all()
            lots = tuple(
                PositionLot(
                    lot_id=lot.id,
                    opening_fill_id=lot.opening_fill_id,
                    opened_at=lot.opened_at,
                    entry_price=lot.entry_price,
                    original_quantity=lot.original_quantity,
                    remaining_quantity=lot.remaining_quantity,
                    opening_fee=lot.opening_fee,
                    remaining_opening_fee=lot.remaining_opening_fee,
                    funding=lot.funding,
                )
                for lot in lot_rows
            )
            positions[row.symbol] = PositionState(
                position_id=row.id,
                experiment_id=row.experiment_id,
                symbol=row.symbol,
                side=Direction(row.side),
                mark_price=row.mark_price,
                average_entry=row.average_entry,
                realized_pnl=row.realized_pnl,
                fees=row.fees,
                funding=row.funding,
                lots=lots,
                opened_at=row.opened_at,
                closed_at=row.closed_at,
                version=row.version,
            )
        leverage = position_rows[0].leverage if position_rows else 1
        account = AccountState(
            experiment_id=experiment_id,
            initial_capital=experiment.initial_capital,
            currency=experiment.currency,
            leverage=leverage,
            cash_balance=snapshot.cash_balance,
            peak_equity=snapshot.peak_equity,
            positions=positions,
            version=snapshot.account_version,
            updated_at=snapshot.snapshot_at,
        )
        if not account.reconcile().ok:
            raise ArithmeticError("restored account does not reconcile")
        return account

    async def load_pending_orders(
        self,
        experiment_id: UUID,
    ) -> tuple[PendingPaperOrder, ...]:
        pending_statuses = (
            PaperOrderStatus.CREATED.value,
            PaperOrderStatus.AUTHORIZED.value,
            PaperOrderStatus.ACCEPTED.value,
            PaperOrderStatus.PARTIALLY_FILLED.value,
        )
        rows = (
            await self._session.scalars(
                select(OrderIntentModel)
                .where(
                    OrderIntentModel.experiment_id == experiment_id,
                    OrderIntentModel.status.in_(pending_statuses),
                )
                .order_by(OrderIntentModel.created_at, OrderIntentModel.id)
            )
        ).all()
        result: list[PendingPaperOrder] = []
        for row in rows:
            event_rows = (
                await self._session.scalars(
                    select(OrderEventModel)
                    .where(OrderEventModel.order_intent_id == row.id)
                    .order_by(OrderEventModel.sequence)
                )
            ).all()
            events = tuple(
                OrderTransition(
                    sequence=event.sequence,
                    event_type=event.event_type,
                    event_at=event.event_at,
                    payload=_event_object(event.payload_json),
                )
                for event in event_rows
            )
            if len(events) != row.version or tuple(item.sequence for item in events) != tuple(
                range(1, row.version + 1)
            ):
                raise StaleExecutionState("pending order event stream is incomplete")
            order = PaperOrder(
                order_id=row.id,
                experiment_id=row.experiment_id,
                proposal_id=row.proposal_id,
                client_order_id=row.client_order_id,
                command_hash=row.command_hash,
                symbol=row.symbol,
                side=PaperOrderSide(row.side),
                order_type=PaperOrderType(row.order_type),
                position_effect=PositionEffect(row.position_effect),
                quantity=row.quantity,
                filled_quantity=row.filled_quantity,
                limit_price=row.limit_price,
                reduce_only=row.reduce_only,
                status=PaperOrderStatus(row.status),
                created_at=row.created_at,
                expires_at=row.expires_at,
                version=row.version,
                events=events,
            )
            result.append(
                PendingPaperOrder(
                    order=order,
                    exchange_filters=_filters_from_json(row.exchange_filter_snapshot_json),
                )
            )
        return tuple(result)

    async def load_position_entry_proposal_id(
        self,
        experiment_id: UUID,
        position_id: UUID,
    ) -> UUID:
        """Restore the proposal that originally opened an active paper position."""

        proposal_id = await self._session.scalar(
            select(OrderIntentModel.proposal_id)
            .select_from(PositionLotModel)
            .join(FillModel, FillModel.id == PositionLotModel.opening_fill_id)
            .join(OrderIntentModel, OrderIntentModel.id == FillModel.order_intent_id)
            .join(PositionModel, PositionModel.id == PositionLotModel.position_id)
            .where(
                PositionModel.id == position_id,
                PositionModel.experiment_id == experiment_id,
                PositionModel.status == "open",
                OrderIntentModel.experiment_id == experiment_id,
                OrderIntentModel.position_effect == PositionEffect.OPEN.value,
            )
            .order_by(PositionLotModel.opened_at, PositionLotModel.id)
            .limit(1)
        )
        if proposal_id is None:
            raise LookupError("open position has no persisted entry proposal lineage")
        return proposal_id

    async def load_open_exit_plans(self, experiment_id: UUID) -> tuple[ExitPlan, ...]:
        rows = (
            await self._session.scalars(
                select(ExitPlanModel)
                .join(PositionModel, PositionModel.id == ExitPlanModel.position_id)
                .where(
                    PositionModel.experiment_id == experiment_id,
                    PositionModel.status == "open",
                    ExitPlanModel.status.in_(
                        (ExitPlanStatus.ACTIVE.value, ExitPlanStatus.TRIGGERED.value)
                    ),
                )
                .order_by(ExitPlanModel.created_at, ExitPlanModel.id)
            )
        ).all()
        return tuple(
            ExitPlan(
                plan_id=row.id,
                position_id=row.position_id,
                side=Direction(row.side),
                quantity=row.quantity,
                average_entry=row.average_entry,
                expected_loss_fraction=row.expected_loss_fraction,
                expected_gain_fraction=row.expected_gain_fraction,
                stop_price=row.stop_price,
                target_price=row.target_price,
                maximum_bars=row.maximum_bars,
                bars_elapsed=row.bars_elapsed,
                opposite_signal_streak=row.opposite_signal_streak,
                status=ExitPlanStatus(row.status),
                created_at=row.created_at,
                changed_at=row.changed_at,
                version=row.version,
                trigger_reason=(
                    ExitReason(row.trigger_reason) if row.trigger_reason is not None else None
                ),
                triggered_at=row.triggered_at,
                trigger_price=row.trigger_price,
                trigger_executable_price=row.trigger_executable_price,
            )
            for row in rows
        )

    async def record_account_state(
        self,
        account: AccountState,
        exit_plan: ExitPlan,
        *,
        source_event_id: str,
        source_event_kind: str,
    ) -> bool:
        """Persist a non-order account transition exactly once by source event."""

        if not source_event_id or not source_event_kind:
            raise ValueError("account state source identity is required")
        if not any(
            position.position_id == exit_plan.position_id for position in account.positions.values()
        ):
            raise ValueError("exit plan position does not exist in account state")
        snapshot_id = uuid5(
            NAMESPACE_URL,
            f"maais://paper-account/{account.experiment_id}/{source_event_kind}/{source_event_id}",
        )
        existing = await self._session.get(AccountSnapshotModel, snapshot_id)
        if existing is not None:
            return False
        latest = await self._session.scalar(
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.experiment_id == account.experiment_id)
            .order_by(AccountSnapshotModel.account_version.desc())
            .limit(1)
            .with_for_update()
        )
        existing = await self._session.get(AccountSnapshotModel, snapshot_id)
        if existing is not None:
            return False
        if latest is None or account.version != latest.account_version + 1:
            raise StaleExecutionState(
                "account state must advance exactly one version from persisted state"
            )
        created = await self._persist_account(
            account,
            exit_plan,
            snapshot_id=snapshot_id,
        )
        if not created:
            raise StaleExecutionState("account source event conflicts with persisted version")
        await self._append_account_snapshot(
            account,
            metadata={
                "source_event_id": source_event_id,
                "source_event_kind": source_event_kind,
                "exit_plan_id": str(exit_plan.plan_id),
            },
        )
        return True

    async def record_sensitivities(
        self,
        order_intent_id: UUID,
        outcomes: tuple[SensitivityOutcome, ...],
    ) -> int:
        order = await self._session.get(OrderIntentModel, order_intent_id)
        if order is None:
            raise LookupError("paper order does not exist")
        if {item.scenario for item in outcomes} != set(SensitivityScenario) or len(outcomes) != 3:
            raise ValueError("exactly one optimistic, conservative, and stress outcome is required")
        created = 0
        for outcome in outcomes:
            aggregate_id = uuid5(
                NAMESPACE_URL,
                f"maais://paper-order/{order_intent_id}/sensitivity/{outcome.scenario.value}",
            )
            payload = _json_object(outcome.to_dict())
            inserted_id = await self._session.scalar(
                insert(ExecutionSensitivityModel)
                .values(
                    id=aggregate_id,
                    order_intent_id=order_intent_id,
                    scenario=outcome.scenario.value,
                    calculated_at=outcome.calculated_at,
                    outcome_json=payload,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ExecutionSensitivityModel.order_intent_id,
                        ExecutionSensitivityModel.scenario,
                    ]
                )
                .returning(ExecutionSensitivityModel.id)
            )
            if inserted_id is None:
                row = await self._session.scalar(
                    select(ExecutionSensitivityModel).where(
                        ExecutionSensitivityModel.order_intent_id == order_intent_id,
                        ExecutionSensitivityModel.scenario == outcome.scenario.value,
                    )
                )
                if row is None or row.outcome_json != payload:
                    raise OrderIdentityConflict(
                        "execution sensitivity identity has different content"
                    )
                continue
            created += 1
            await self._events.append(
                aggregate_id,
                "execution_sensitivity",
                0,
                (
                    NewDomainEvent(
                        aggregate_id=aggregate_id,
                        aggregate_type="execution_sensitivity",
                        event_type="execution_sensitivity.recorded",
                        payload=_event_object(outcome.to_dict()),
                        metadata={
                            "experiment_id": str(order.experiment_id),
                            "order_intent_id": str(order_intent_id),
                        },
                        occurred_at=outcome.calculated_at,
                    ),
                ),
            )
        return created

    async def record_funding(self, funding: FundingRecord, account: AccountState) -> bool:
        if funding.experiment_id != account.experiment_id:
            raise ValueError("funding and account experiment differ")
        position = next(
            (
                item
                for item in account.positions.values()
                if item.position_id == funding.position_id
            ),
            None,
        )
        if position is None:
            raise ValueError("funding position does not exist in account state")
        if funding.mark_price != position.mark_price:
            raise ValueError("funding mark price differs from official position mark")
        if funding.notional != position.gross_notional:
            raise ValueError("funding notional differs from official position notional")
        expected_amount = funding.notional * funding.rate
        if position.side is Direction.LONG:
            expected_amount = -expected_amount
        if expected_amount != funding.amount:
            raise ValueError("funding amount does not match side, notional, and rate")
        latest = await self._session.scalar(
            select(AccountSnapshotModel)
            .where(AccountSnapshotModel.experiment_id == account.experiment_id)
            .order_by(AccountSnapshotModel.account_version.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is None:
            raise ValueError("funding requires an existing official account snapshot")
        if account.version <= latest.account_version:
            existing = await self._session.scalar(
                select(FundingEntryModel).where(
                    FundingEntryModel.experiment_id == funding.experiment_id,
                    FundingEntryModel.market_event_id == funding.market_event_id,
                )
            )
            if existing is not None and self._same_funding(existing, funding):
                return False
            raise StaleExecutionState("funding account state is not newer than persisted state")
        if account.funding - latest.funding != funding.amount:
            raise ValueError("funding delta does not reconcile to the prior snapshot")
        if account.cash_balance - latest.cash_balance != funding.amount:
            raise ValueError("funding cash delta does not reconcile to the prior snapshot")
        inserted_id = await self._session.scalar(
            insert(FundingEntryModel)
            .values(
                id=funding.id,
                experiment_id=funding.experiment_id,
                position_id=funding.position_id,
                funding_at=funding.funding_at,
                observed_at=funding.observed_at,
                rate=funding.rate,
                rate_type=funding.rate_type,
                mark_price=funding.mark_price,
                notional=funding.notional,
                amount=funding.amount,
                market_event_id=funding.market_event_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    FundingEntryModel.experiment_id,
                    FundingEntryModel.market_event_id,
                ]
            )
            .returning(FundingEntryModel.id)
        )
        if inserted_id is None:
            existing = await self._session.scalar(
                select(FundingEntryModel).where(
                    FundingEntryModel.experiment_id == funding.experiment_id,
                    FundingEntryModel.market_event_id == funding.market_event_id,
                )
            )
            if existing is not None and self._same_funding(existing, funding):
                return False
            raise OrderIdentityConflict("funding market event has different content")
        account_snapshot_created = await self._persist_account(account, None)
        if not account_snapshot_created:
            raise StaleExecutionState("funding account snapshot already exists")
        await self._events.append(
            funding.id,
            "paper_funding",
            0,
            (
                NewDomainEvent(
                    aggregate_id=funding.id,
                    aggregate_type="paper_funding",
                    event_type="paper_funding.recorded",
                    payload=_event_object(
                        {
                            "position_id": funding.position_id,
                            "market_event_id": funding.market_event_id,
                            "funding_at": funding.funding_at,
                            "observed_at": funding.observed_at,
                            "rate": funding.rate,
                            "rate_type": funding.rate_type,
                            "mark_price": funding.mark_price,
                            "notional": funding.notional,
                            "amount": funding.amount,
                        }
                    ),
                    metadata={"experiment_id": str(funding.experiment_id)},
                    occurred_at=funding.observed_at,
                ),
            ),
        )
        await self._append_account_snapshot(
            account,
            metadata={
                "funding_entry_id": str(funding.id),
                "market_event_id": funding.market_event_id,
            },
        )
        return True

    async def load_funding_event(
        self,
        experiment_id: UUID,
        market_event_id: str,
    ) -> FundingRecord | None:
        row = await self._session.scalar(
            select(FundingEntryModel).where(
                FundingEntryModel.experiment_id == experiment_id,
                FundingEntryModel.market_event_id == market_event_id,
            )
        )
        if row is None:
            return None
        return FundingRecord(
            id=row.id,
            experiment_id=row.experiment_id,
            position_id=row.position_id,
            market_event_id=row.market_event_id,
            funding_at=row.funding_at,
            observed_at=row.observed_at,
            rate=row.rate,
            rate_type=row.rate_type,
            mark_price=row.mark_price,
            notional=row.notional,
            amount=row.amount,
        )

    @staticmethod
    def _order_values(record: PaperExecutionRecord, execution_hash: str) -> dict[str, object]:
        order = record.order
        return {
            "id": order.order_id,
            "proposal_id": order.proposal_id,
            "experiment_id": order.experiment_id,
            "client_order_id": order.client_order_id,
            "command_hash": order.command_hash,
            "content_hash": execution_hash,
            "symbol": order.symbol,
            "side": order.side.value,
            "position_effect": order.position_effect.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
            "filled_quantity": order.filled_quantity,
            "limit_price": order.limit_price,
            "reduce_only": order.reduce_only,
            "created_at": order.created_at,
            "expires_at": order.expires_at,
            "status": order.status.value,
            "exchange_filter_snapshot_json": _json_object(_filters_dict(record.exchange_filters)),
            "version": order.version,
        }

    @staticmethod
    def _fill_values(fill: ExecutionFillRecord) -> dict[str, object]:
        return {
            "id": fill.id,
            "order_intent_id": fill.order_intent_id,
            "market_event_id": fill.market_event_id,
            "fill_at": fill.fill_at,
            "quantity": fill.quantity,
            "price": fill.price,
            "liquidity_role": fill.liquidity_role,
            "fee": fill.fee,
            "fee_asset": fill.fee_asset,
            "spread_cost": fill.spread_cost,
            "depth_slippage": fill.depth_slippage,
            "latency_slippage": fill.latency_slippage,
            "total_slippage": fill.total_slippage,
            "market_snapshot_json": _json_object(fill.market_snapshot),
        }

    @staticmethod
    def _same_fill(row: FillModel, fill: ExecutionFillRecord) -> bool:
        return (
            row.order_intent_id == fill.order_intent_id
            and row.market_event_id == fill.market_event_id
            and row.fill_at == fill.fill_at
            and row.quantity == fill.quantity
            and row.price == fill.price
            and row.liquidity_role == fill.liquidity_role
            and row.fee == fill.fee
            and row.fee_asset == fill.fee_asset
            and row.spread_cost == fill.spread_cost
            and row.depth_slippage == fill.depth_slippage
            and row.latency_slippage == fill.latency_slippage
            and row.total_slippage == fill.total_slippage
            and row.market_snapshot_json == _json_object(fill.market_snapshot)
        )

    @staticmethod
    def _same_funding(row: FundingEntryModel, funding: FundingRecord) -> bool:
        return (
            row.id == funding.id
            and row.position_id == funding.position_id
            and row.funding_at == funding.funding_at
            and row.observed_at == funding.observed_at
            and row.rate == funding.rate
            and row.rate_type == funding.rate_type
            and row.mark_price == funding.mark_price
            and row.notional == funding.notional
            and row.amount == funding.amount
        )

    async def _persist_account(
        self,
        account: AccountState,
        exit_plan: ExitPlan | None,
        *,
        snapshot_id: UUID | None = None,
    ) -> bool:
        for position in account.positions.values():
            row = await self._session.get(PositionModel, position.position_id)
            values = self._position_values(position, account.leverage)
            if row is None:
                self._session.add(PositionModel(**values))
            else:
                if position.version < row.version:
                    raise StaleExecutionState("position state is older than persisted state")
                for key, value in values.items():
                    if key != "id":
                        setattr(row, key, value)
        await self._session.flush()
        for position in account.positions.values():
            for lot in position.lots:
                await self._session.execute(
                    insert(PositionLotModel)
                    .values(
                        id=lot.lot_id,
                        position_id=position.position_id,
                        opening_fill_id=lot.opening_fill_id,
                        opened_at=lot.opened_at,
                        entry_price=lot.entry_price,
                        original_quantity=lot.original_quantity,
                        remaining_quantity=lot.remaining_quantity,
                        opening_fee=lot.opening_fee,
                        remaining_opening_fee=lot.remaining_opening_fee,
                        funding=lot.funding,
                    )
                    .on_conflict_do_update(
                        index_elements=[PositionLotModel.id],
                        set_={
                            "remaining_quantity": lot.remaining_quantity,
                            "remaining_opening_fee": lot.remaining_opening_fee,
                            "funding": lot.funding,
                        },
                    )
                )
        if exit_plan is not None:
            values = self._exit_values(exit_plan)
            row = await self._session.get(ExitPlanModel, exit_plan.plan_id)
            if row is None:
                self._session.add(ExitPlanModel(**values))
            elif exit_plan.version >= row.version:
                for key, value in values.items():
                    if key != "id":
                        setattr(row, key, value)
            else:
                raise StaleExecutionState("exit plan is older than persisted state")
        snapshot_at = account.updated_at
        if snapshot_at is None:
            raise ValueError("persisted account must have an update timestamp")
        snapshot = account.snapshot()
        risk_at_stop = Decimal("0")
        if exit_plan is not None and exit_plan.status in {
            ExitPlanStatus.ACTIVE,
            ExitPlanStatus.TRIGGERED,
        }:
            risk_at_stop = abs(exit_plan.average_entry - exit_plan.stop_price) * exit_plan.quantity
        elif account.positions:
            active_exit = await self._session.scalar(
                select(ExitPlanModel).where(
                    ExitPlanModel.position_id.in_(
                        position.position_id for position in account.positions.values()
                    ),
                    ExitPlanModel.status.in_(
                        (ExitPlanStatus.ACTIVE.value, ExitPlanStatus.TRIGGERED.value)
                    ),
                )
            )
            if active_exit is not None:
                risk_at_stop = (
                    abs(active_exit.average_entry - active_exit.stop_price) * active_exit.quantity
                )
        snapshot_id = await self._session.scalar(
            insert(AccountSnapshotModel)
            .values(
                id=snapshot_id or uuid4(),
                experiment_id=account.experiment_id,
                account_version=account.version,
                snapshot_at=snapshot_at,
                cash_balance=snapshot.cash_balance,
                equity=snapshot.equity,
                used_margin=snapshot.used_margin,
                free_margin=snapshot.free_margin,
                gross_notional=snapshot.gross_notional,
                risk_at_stop=risk_at_stop,
                unrealized_pnl=snapshot.unrealized_pnl,
                realized_pnl=snapshot.realized_pnl,
                fees=snapshot.fees,
                funding=snapshot.funding,
                peak_equity=snapshot.peak_equity,
                drawdown=snapshot.drawdown,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    AccountSnapshotModel.experiment_id,
                    AccountSnapshotModel.account_version,
                ]
            )
            .returning(AccountSnapshotModel.id)
        )
        await self._session.flush()
        return snapshot_id is not None

    @staticmethod
    def _position_values(position: PositionState, leverage: int) -> dict[str, object]:
        status = "closed" if position.is_flat else "open"
        initial_margin = position.gross_notional / Decimal(leverage)
        return {
            "id": position.position_id,
            "experiment_id": position.experiment_id,
            "symbol": position.symbol,
            "side": position.side.value,
            "status": status,
            "quantity": position.quantity,
            "average_entry": position.average_entry,
            "mark_price": position.mark_price,
            "initial_margin": initial_margin,
            "maintenance_margin": position.gross_notional * Decimal("0.005"),
            "leverage": leverage,
            "unrealized_pnl": position.unrealized_pnl,
            "realized_pnl": position.realized_pnl,
            "fees": position.fees,
            "funding": position.funding,
            "opened_at": position.opened_at,
            "closed_at": position.closed_at,
            "version": position.version,
        }

    @staticmethod
    def _exit_values(plan: ExitPlan) -> dict[str, object]:
        return {
            "id": plan.plan_id,
            "position_id": plan.position_id,
            "version": plan.version,
            "status": plan.status.value,
            "side": plan.side.value,
            "quantity": plan.quantity,
            "average_entry": plan.average_entry,
            "expected_loss_fraction": plan.expected_loss_fraction,
            "expected_gain_fraction": plan.expected_gain_fraction,
            "stop_price": plan.stop_price,
            "target_price": plan.target_price,
            "maximum_bars": plan.maximum_bars,
            "bars_elapsed": plan.bars_elapsed,
            "opposite_signal_streak": plan.opposite_signal_streak,
            "created_at": plan.created_at,
            "changed_at": plan.changed_at,
            "trigger_reason": plan.trigger_reason.value
            if plan.trigger_reason is not None
            else None,
            "triggered_at": plan.triggered_at,
            "trigger_price": plan.trigger_price,
            "trigger_executable_price": plan.trigger_executable_price,
        }

    async def _append_events(
        self,
        record: PaperExecutionRecord,
        *,
        previous_order_version: int,
        new_transitions: tuple[OrderTransition, ...],
        created_fills: tuple[ExecutionFillRecord, ...],
        account_snapshot_created: bool,
    ) -> None:
        order = record.order
        order_events = tuple(
            NewDomainEvent(
                aggregate_id=order.order_id,
                aggregate_type="paper_order",
                event_type=transition.event_type,
                payload=transition.payload,
                metadata={
                    "experiment_id": str(order.experiment_id),
                    "proposal_id": str(order.proposal_id),
                },
                occurred_at=transition.event_at,
            )
            for transition in new_transitions
        )
        await self._events.append(
            order.order_id,
            "paper_order",
            previous_order_version,
            order_events,
        )
        for fill in created_fills:
            await self._events.append(
                fill.id,
                "paper_fill",
                0,
                (
                    NewDomainEvent(
                        aggregate_id=fill.id,
                        aggregate_type="paper_fill",
                        event_type="paper_fill.recorded",
                        payload=_event_object(fill.to_dict()),
                        metadata={
                            "experiment_id": str(order.experiment_id),
                            "order_intent_id": str(order.order_id),
                        },
                        occurred_at=fill.fill_at,
                    ),
                ),
            )
        if not account_snapshot_created or record.account is None:
            return
        await self._append_account_snapshot(
            record.account,
            metadata={"order_intent_id": str(order.order_id)},
        )

    async def _append_account_snapshot(
        self,
        account: AccountState,
        *,
        metadata: Mapping[str, JsonValue],
    ) -> None:
        account_version = await self._events.stream_version(account.experiment_id, "paper_account")
        snapshot_at = account.updated_at
        assert snapshot_at is not None
        await self._events.append(
            account.experiment_id,
            "paper_account",
            account_version,
            (
                NewDomainEvent(
                    aggregate_id=account.experiment_id,
                    aggregate_type="paper_account",
                    event_type="paper_account.snapshotted",
                    payload=_event_object(
                        {
                            "account_version": account.version,
                            "snapshot": _snapshot_dict(account),
                        }
                    ),
                    metadata=metadata,
                    occurred_at=snapshot_at,
                ),
            ),
        )
