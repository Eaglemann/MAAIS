from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.decisions import TradeProposalModel
from maais.db.repositories.events import EventRepository
from maais.domain.enums import Direction, GateType, PaperOrderSide
from maais.domain.events import NewDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, content_hash, freeze_json, to_json_data
from maais.execution.paper.exits import ExitPlan, ExitPlanStatus, ExitReason
from maais.execution.paper.fills import FillSlice, PaperFill
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.research.counterfactuals import (
    CounterfactualState,
    CounterfactualStatus,
    CounterfactualTransition,
    HorizonOutcome,
)


class CounterfactualIdentityConflict(RuntimeError):
    pass


class StaleCounterfactualState(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CounterfactualRecordResult:
    created: bool
    counterfactual_id: UUID
    version: int
    content_hash: str


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):
        raise TypeError("expected JSON object")
    return normalized


def _event_object(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("expected event object")
    return normalized


def _book_dict(book: BookSnapshot) -> dict[str, object]:
    return {
        "event_id": book.event_id,
        "symbol": book.symbol,
        "venue_event_at": book.venue_event_at,
        "observed_at": book.observed_at,
        "sequence": book.sequence,
        "bids": [{"price": level.price, "quantity": level.quantity} for level in book.bids],
        "asks": [{"price": level.price, "quantity": level.quantity} for level in book.asks],
        "mark_price": book.mark_price,
    }


def _fill_dict(fill: PaperFill) -> dict[str, object]:
    return {
        "market_event_id": fill.market_event_id,
        "symbol": fill.symbol,
        "side": fill.side,
        "fill_at": fill.fill_at,
        "quantity": fill.quantity,
        "price": fill.price,
        "notional": fill.notional,
        "slices": [{"price": item.price, "quantity": item.quantity} for item in fill.slices],
        "liquidity_role": fill.liquidity_role,
        "fee": fill.fee,
        "spread_cost": fill.spread_cost,
        "depth_slippage": fill.depth_slippage,
        "latency_slippage": fill.latency_slippage,
        "total_slippage": fill.total_slippage,
        "book": _book_dict(fill.book),
    }


def _exit_dict(plan: ExitPlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "position_id": plan.position_id,
        "side": plan.side,
        "quantity": plan.quantity,
        "average_entry": plan.average_entry,
        "expected_loss_fraction": plan.expected_loss_fraction,
        "expected_gain_fraction": plan.expected_gain_fraction,
        "stop_price": plan.stop_price,
        "target_price": plan.target_price,
        "maximum_bars": plan.maximum_bars,
        "bars_elapsed": plan.bars_elapsed,
        "opposite_signal_streak": plan.opposite_signal_streak,
        "status": plan.status,
        "created_at": plan.created_at,
        "changed_at": plan.changed_at,
        "version": plan.version,
        "trigger_reason": plan.trigger_reason,
        "triggered_at": plan.triggered_at,
        "trigger_price": plan.trigger_price,
        "trigger_executable_price": plan.trigger_executable_price,
    }


def state_to_dict(state: CounterfactualState) -> dict[str, object]:
    return {
        "counterfactual_id": state.counterfactual_id,
        "experiment_id": state.experiment_id,
        "proposal_id": state.proposal_id,
        "decision_cycle_id": state.decision_cycle_id,
        "symbol": state.symbol,
        "direction": state.direction,
        "rejection_gate": state.rejection_gate,
        "prior_gate_chain": state.prior_gate_chain,
        "quantity": state.quantity,
        "decision_executable_price": state.decision_executable_price,
        "eligible_after": state.eligible_after,
        "fee_rate": state.fee_rate,
        "expected_loss_fraction": state.expected_loss_fraction,
        "expected_gain_fraction": state.expected_gain_fraction,
        "status": state.status,
        "entry_fill": _fill_dict(state.entry_fill) if state.entry_fill else None,
        "exit_plan": _exit_dict(state.exit_plan) if state.exit_plan else None,
        "maximum_favorable_excursion": state.maximum_favorable_excursion,
        "maximum_adverse_excursion": state.maximum_adverse_excursion,
        "outcomes": [
            {
                "horizon": item.horizon,
                "observed_at": item.observed_at,
                "mark_price": item.mark_price,
                "net_pnl": item.net_pnl,
            }
            for item in state.outcomes
        ],
        "funding": state.funding,
        "no_fill_reason": state.no_fill_reason,
        "hypothetical_exit_reason": state.hypothetical_exit_reason,
        "hypothetical_pnl": state.hypothetical_pnl,
        "created_at": state.created_at,
        "closed_at": state.closed_at,
        "version": state.version,
        "events": [
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "event_at": event.event_at,
                "payload": event.payload,
            }
            for event in state.events
        ],
    }


class CounterfactualRepository:
    """Research-only repository; it has no official account dependency."""

    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def record(self, state: CounterfactualState) -> CounterfactualRecordResult:
        proposal = await self._session.get(TradeProposalModel, state.proposal_id)
        if proposal is None:
            raise ValueError("counterfactual proposal does not exist")
        if proposal.status != "rejected":
            raise ValueError("counterfactuals require a rejected directional proposal")
        if (
            proposal.experiment_id != state.experiment_id
            or proposal.decision_cycle_id != state.decision_cycle_id
            or proposal.symbol != state.symbol
            or proposal.direction != state.direction.value
        ):
            raise ValueError("counterfactual identity differs from rejected proposal")
        state_json = state_to_dict(state)
        state_hash = content_hash(state_json)
        inserted_id = await self._session.scalar(
            insert(CounterfactualModel)
            .values(**self._values(state, state_hash, state_json))
            .on_conflict_do_nothing(index_elements=[CounterfactualModel.proposal_id])
            .returning(CounterfactualModel.id)
        )
        created = inserted_id is not None
        previous_version = 0
        if created:
            new_events = state.events
        else:
            row = await self._session.scalar(
                select(CounterfactualModel)
                .where(CounterfactualModel.proposal_id == state.proposal_id)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("counterfactual disappeared after identity conflict")
            if row.id != state.counterfactual_id:
                raise CounterfactualIdentityConflict(
                    "proposal already has a different counterfactual identity"
                )
            previous_version = row.version
            if state.version < previous_version:
                raise StaleCounterfactualState("state is older than persisted projection")
            if state.version == previous_version:
                if row.content_hash != state_hash:
                    raise CounterfactualIdentityConflict(
                        "same counterfactual version has different content"
                    )
                return CounterfactualRecordResult(False, row.id, row.version, state_hash)
            new_events = tuple(event for event in state.events if event.sequence > previous_version)
            if not new_events or new_events[0].sequence != previous_version + 1:
                raise StaleCounterfactualState("event sequence is not contiguous")
            for key, value in self._values(state, state_hash, state_json).items():
                if key != "id":
                    setattr(row, key, value)
        await self._session.flush()
        await self._events.append(
            state.counterfactual_id,
            "counterfactual",
            previous_version,
            tuple(
                NewDomainEvent(
                    aggregate_id=state.counterfactual_id,
                    aggregate_type="counterfactual",
                    event_type=event.event_type,
                    payload=event.payload,
                    metadata={
                        "experiment_id": str(state.experiment_id),
                        "proposal_id": str(state.proposal_id),
                        "decision_cycle_id": str(state.decision_cycle_id),
                    },
                    occurred_at=event.event_at,
                )
                for event in new_events
            ),
        )
        return CounterfactualRecordResult(
            created, state.counterfactual_id, state.version, state_hash
        )

    async def get(self, counterfactual_id: UUID) -> CounterfactualState:
        row = await self._session.get(CounterfactualModel, counterfactual_id)
        if row is None:
            raise LookupError("counterfactual does not exist")
        state = _state_from_json(row.state_json)
        if content_hash(state_to_dict(state)) != row.content_hash:
            raise CounterfactualIdentityConflict("counterfactual projection hash differs")
        return state

    async def get_unresolved(
        self,
        experiment_id: UUID,
    ) -> tuple[CounterfactualState, ...]:
        rows = (
            await self._session.scalars(
                select(CounterfactualModel)
                .where(
                    CounterfactualModel.experiment_id == experiment_id,
                    CounterfactualModel.status.in_(
                        (
                            CounterfactualStatus.PENDING.value,
                            CounterfactualStatus.OPEN.value,
                        )
                    ),
                )
                .order_by(CounterfactualModel.created_at, CounterfactualModel.id)
            )
        ).all()
        states: list[CounterfactualState] = []
        for row in rows:
            state = _state_from_json(row.state_json)
            if content_hash(state_to_dict(state)) != row.content_hash:
                raise CounterfactualIdentityConflict("counterfactual projection hash differs")
            states.append(state)
        return tuple(states)

    @staticmethod
    def _values(
        state: CounterfactualState,
        state_hash: str,
        state_json: dict[str, object],
    ) -> dict[str, object]:
        return {
            "id": state.counterfactual_id,
            "experiment_id": state.experiment_id,
            "proposal_id": state.proposal_id,
            "decision_cycle_id": state.decision_cycle_id,
            "symbol": state.symbol,
            "direction": state.direction.value,
            "rejection_gate": state.rejection_gate.value,
            "prior_gate_chain_json": to_json_data(state.prior_gate_chain),
            "status": state.status.value,
            "quantity": state.quantity,
            "decision_executable_price": state.decision_executable_price,
            "eligible_after": state.eligible_after,
            "fee_rate": state.fee_rate,
            "expected_loss_fraction": state.expected_loss_fraction,
            "expected_gain_fraction": state.expected_gain_fraction,
            "hypothetical_fill_json": (
                _json_object(_fill_dict(state.entry_fill)) if state.entry_fill else None
            ),
            "exit_policy_json": (
                _json_object(_exit_dict(state.exit_plan)) if state.exit_plan else None
            ),
            "maximum_favorable_excursion": state.maximum_favorable_excursion,
            "maximum_adverse_excursion": state.maximum_adverse_excursion,
            "outcome_15m": state.outcome("15m"),
            "outcome_1h": state.outcome("1h"),
            "outcome_4h": state.outcome("4h"),
            "outcome_24h": state.outcome("24h"),
            "funding": state.funding,
            "no_fill_reason": state.no_fill_reason,
            "hypothetical_exit_reason": state.hypothetical_exit_reason,
            "hypothetical_pnl": state.hypothetical_pnl,
            "created_at": state.created_at,
            "closed_at": state.closed_at,
            "version": state.version,
            "content_hash": state_hash,
            "state_json": _json_object(state_json),
        }


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("stored datetime must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _uuid(value: object) -> UUID:
    return UUID(str(value))


def _integer(value: object) -> int:
    return int(str(value))


def _mapping(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("stored payload must be an object")
    return normalized


def _state_from_json(data: Mapping[str, object]) -> CounterfactualState:
    fill_data = data.get("entry_fill")
    fill = _fill_from_json(fill_data) if isinstance(fill_data, Mapping) else None
    plan_data = data.get("exit_plan")
    plan = _exit_from_json(plan_data) if isinstance(plan_data, Mapping) else None
    outcomes_data = data.get("outcomes", [])
    events_data = data.get("events", [])
    if not isinstance(outcomes_data, list) or not isinstance(events_data, list):
        raise TypeError("stored outcomes and events must be lists")
    return CounterfactualState(
        counterfactual_id=_uuid(data["counterfactual_id"]),
        experiment_id=_uuid(data["experiment_id"]),
        proposal_id=_uuid(data["proposal_id"]),
        decision_cycle_id=_uuid(data["decision_cycle_id"]),
        symbol=str(data["symbol"]),
        direction=Direction(str(data["direction"])),
        rejection_gate=GateType(str(data["rejection_gate"])),
        prior_gate_chain=tuple(GateType(str(item)) for item in data["prior_gate_chain"]),  # type: ignore[union-attr]
        quantity=_decimal(data["quantity"]),
        decision_executable_price=_decimal(data["decision_executable_price"]),
        eligible_after=_datetime(data["eligible_after"]),
        fee_rate=_decimal(data["fee_rate"]),
        expected_loss_fraction=_decimal(data["expected_loss_fraction"]),
        expected_gain_fraction=_decimal(data["expected_gain_fraction"]),
        status=CounterfactualStatus(str(data["status"])),
        entry_fill=fill,
        exit_plan=plan,
        maximum_favorable_excursion=_decimal(data["maximum_favorable_excursion"]),
        maximum_adverse_excursion=_decimal(data["maximum_adverse_excursion"]),
        outcomes=tuple(
            HorizonOutcome(
                horizon=str(item["horizon"]),
                observed_at=_datetime(item["observed_at"]),
                mark_price=_decimal(item["mark_price"]),
                net_pnl=_decimal(item["net_pnl"]),
            )
            for item in outcomes_data
            if isinstance(item, Mapping)
        ),
        funding=_decimal(data["funding"]),
        no_fill_reason=(str(data["no_fill_reason"]) if data.get("no_fill_reason") else None),
        hypothetical_exit_reason=(
            str(data["hypothetical_exit_reason"]) if data.get("hypothetical_exit_reason") else None
        ),
        hypothetical_pnl=(
            _decimal(data["hypothetical_pnl"]) if data.get("hypothetical_pnl") is not None else None
        ),
        created_at=_datetime(data["created_at"]),
        closed_at=_datetime(data["closed_at"]) if data.get("closed_at") else None,
        version=_integer(data["version"]),
        events=tuple(
            CounterfactualTransition(
                sequence=_integer(item["sequence"]),
                event_type=str(item["event_type"]),
                event_at=_datetime(item["event_at"]),
                payload=_mapping(item["payload"]),
            )
            for item in events_data
            if isinstance(item, Mapping)
        ),
    )


def _fill_from_json(data: Mapping[str, object]) -> PaperFill:
    book_data = data["book"]
    if not isinstance(book_data, Mapping):
        raise TypeError("stored book must be an object")
    bids = book_data["bids"]
    asks = book_data["asks"]
    slices = data["slices"]
    if not isinstance(bids, list) or not isinstance(asks, list) or not isinstance(slices, list):
        raise TypeError("stored levels and slices must be lists")
    book = BookSnapshot(
        event_id=str(book_data["event_id"]),
        symbol=str(book_data["symbol"]),
        venue_event_at=_datetime(book_data["venue_event_at"]),
        observed_at=_datetime(book_data["observed_at"]),
        sequence=_integer(book_data["sequence"]),
        bids=tuple(
            BookLevel(_decimal(item["price"]), _decimal(item["quantity"]))
            for item in bids
            if isinstance(item, Mapping)
        ),
        asks=tuple(
            BookLevel(_decimal(item["price"]), _decimal(item["quantity"]))
            for item in asks
            if isinstance(item, Mapping)
        ),
        mark_price=_decimal(book_data["mark_price"]),
    )
    return PaperFill(
        market_event_id=str(data["market_event_id"]),
        symbol=str(data["symbol"]),
        side=PaperOrderSide(str(data["side"])),
        fill_at=_datetime(data["fill_at"]),
        quantity=_decimal(data["quantity"]),
        price=_decimal(data["price"]),
        notional=_decimal(data["notional"]),
        slices=tuple(
            FillSlice(_decimal(item["price"]), _decimal(item["quantity"]))
            for item in slices
            if isinstance(item, Mapping)
        ),
        liquidity_role=str(data["liquidity_role"]),
        fee=_decimal(data["fee"]),
        spread_cost=_decimal(data["spread_cost"]),
        depth_slippage=_decimal(data["depth_slippage"]),
        latency_slippage=_decimal(data["latency_slippage"]),
        total_slippage=_decimal(data["total_slippage"]),
        book=book,
    )


def _exit_from_json(data: Mapping[str, object]) -> ExitPlan:
    status = ExitPlanStatus(str(data["status"]))
    trigger_reason = data.get("trigger_reason")
    triggered_at = data.get("triggered_at")
    trigger_executable_price = data.get("trigger_executable_price")
    return ExitPlan(
        plan_id=_uuid(data["plan_id"]),
        position_id=_uuid(data["position_id"]),
        side=Direction(str(data["side"])),
        quantity=_decimal(data["quantity"]),
        average_entry=_decimal(data["average_entry"]),
        expected_loss_fraction=_decimal(data["expected_loss_fraction"]),
        expected_gain_fraction=_decimal(data["expected_gain_fraction"]),
        stop_price=_decimal(data["stop_price"]),
        target_price=_decimal(data["target_price"]),
        maximum_bars=_integer(data["maximum_bars"]),
        bars_elapsed=_integer(data["bars_elapsed"]),
        opposite_signal_streak=_integer(data["opposite_signal_streak"]),
        status=status,
        created_at=_datetime(data["created_at"]),
        changed_at=_datetime(data["changed_at"]),
        version=_integer(data["version"]),
        trigger_reason=(ExitReason(str(trigger_reason)) if trigger_reason is not None else None),
        triggered_at=(_datetime(triggered_at) if triggered_at is not None else None),
        trigger_price=(
            _decimal(data["trigger_price"]) if data.get("trigger_price") is not None else None
        ),
        trigger_executable_price=(
            _decimal(trigger_executable_price) if trigger_executable_price is not None else None
        ),
    )
