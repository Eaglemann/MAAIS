from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.config.constants import ALL_AGENTS
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    DecisionSummaryModel,
    GateEvaluationModel,
    MarketFrameModel,
    TradeProposalModel,
)
from maais.db.models.experiments import AgentVersionModel, ExperimentModel
from maais.db.repositories.events import EventRepository
from maais.decisions.bundle import (
    AgentEvaluationRecord,
    DecisionBundle,
    DecisionCycleRecord,
    DecisionSummaryRecord,
    GateEvaluationRecord,
    MarketFrameRecord,
    TradeProposalRecord,
    record_to_dict,
)
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    ProposalStatus,
    QualityStatus,
    ReasonCode,
)
from maais.domain.events import NewDomainEvent, StoredDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, freeze_json, to_json_data


class DecisionIdentityConflict(RuntimeError):
    pass


class IncompleteDecisionBundleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DecisionRecordResult:
    created: bool
    decision_cycle_id: UUID
    content_hash: str


@dataclass(frozen=True, slots=True)
class DecisionBundleView:
    bundle: DecisionBundle
    events: tuple[StoredDomainEvent, ...]
    config_hash: str
    manifest_hash: str


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):
        raise TypeError("expected a JSON object")
    return normalized


def _event_payload(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("expected an immutable JSON object")
    return normalized


class DecisionRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def record_bundle(self, bundle: DecisionBundle) -> DecisionRecordResult:
        bundle.validate()
        bundle_hash = bundle.bundle_hash
        frame = bundle.market_frame
        cycle = bundle.cycle

        inserted_frame_id = await self._session.scalar(
            insert(MarketFrameModel)
            .values(
                id=frame.id,
                experiment_id=frame.experiment_id,
                symbol=frame.symbol,
                venue=frame.venue,
                timeframe=frame.timeframe,
                bar_open_at=frame.bar_open_at,
                bar_close_at=frame.bar_close_at,
                observed_at=frame.observed_at,
                open=frame.open,
                high=frame.high,
                low=frame.low,
                close=frame.close,
                volume=frame.volume,
                best_bid=frame.best_bid,
                best_ask=frame.best_ask,
                mark_price=frame.mark_price,
                index_price=frame.index_price,
                funding_rate=frame.funding_rate,
                primary_spot_price=frame.primary_spot_price,
                secondary_venue_price=frame.secondary_venue_price,
                bar_snapshot_json=_json_object(frame.bar_snapshot),
                orderbook_snapshot_json=_json_object(frame.orderbook_snapshot),
                source_manifest_json=_json_object(frame.source_manifest),
                source_sequence_json=_json_object(frame.source_sequence),
                quality_status=frame.quality_status.value,
                quality_results_json=_json_object(frame.quality_results),
                content_hash=frame.content_hash,
            )
            .on_conflict_do_nothing(
                index_elements=[MarketFrameModel.experiment_id, MarketFrameModel.content_hash]
            )
            .returning(MarketFrameModel.id)
        )
        frame_created = inserted_frame_id is not None
        if not frame_created:
            existing_frame_id = await self._session.scalar(
                select(MarketFrameModel.id).where(
                    MarketFrameModel.experiment_id == frame.experiment_id,
                    MarketFrameModel.content_hash == frame.content_hash,
                )
            )
            if existing_frame_id != frame.id:
                raise DecisionIdentityConflict("market frame content has a different identity")

        inserted_cycle_id = await self._session.scalar(
            insert(DecisionCycleModel)
            .values(
                id=cycle.id,
                experiment_id=cycle.experiment_id,
                market_frame_id=cycle.market_frame_id,
                strategy_version_id=cycle.strategy_version_id,
                symbol=cycle.symbol,
                timeframe=cycle.timeframe,
                cycle_at=cycle.cycle_at,
                regime=cycle.regime,
                feature_snapshot_json=_json_object(cycle.feature_snapshot),
                feature_version=cycle.feature_version,
                status=cycle.status.value,
                direction=cycle.direction.value,
                disposition=cycle.disposition.value,
                reason_code=cycle.reason_code.value,
                content_hash=bundle_hash,
                created_at=cycle.created_at,
                completed_at=cycle.completed_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    DecisionCycleModel.experiment_id,
                    DecisionCycleModel.symbol,
                    DecisionCycleModel.timeframe,
                    DecisionCycleModel.cycle_at,
                    DecisionCycleModel.strategy_version_id,
                ]
            )
            .returning(DecisionCycleModel.id)
        )
        if inserted_cycle_id is None:
            existing = await self._session.scalar(
                select(DecisionCycleModel).where(
                    DecisionCycleModel.experiment_id == cycle.experiment_id,
                    DecisionCycleModel.symbol == cycle.symbol,
                    DecisionCycleModel.timeframe == cycle.timeframe,
                    DecisionCycleModel.cycle_at == cycle.cycle_at,
                    DecisionCycleModel.strategy_version_id == cycle.strategy_version_id,
                )
            )
            if existing is None:
                raise RuntimeError("decision identity disappeared after conflict")
            if existing.content_hash != bundle_hash:
                raise DecisionIdentityConflict(
                    "decision key already exists with different complete content"
                )
            return DecisionRecordResult(False, existing.id, existing.content_hash)

        for agent in bundle.agents:
            self._session.add(
                AgentEvaluationModel(
                    id=agent.id,
                    decision_cycle_id=agent.decision_cycle_id,
                    agent_version_id=agent.agent_version_id,
                    compatible=agent.compatible,
                    enabled=agent.enabled,
                    weight=agent.weight,
                    direction=agent.direction.value,
                    probability=agent.probability,
                    confidence=agent.confidence,
                    risk=agent.risk,
                    input_snapshot_json=_json_object(agent.input_snapshot),
                    reason_codes_json=[reason.value for reason in agent.reason_codes],
                    explanation_json=_json_object(agent.explanation),
                    duration_ms=agent.duration_ms,
                    created_at=agent.created_at,
                )
            )
        summary = bundle.summary
        self._session.add(
            DecisionSummaryModel(
                decision_cycle_id=summary.decision_cycle_id,
                consensus_direction=summary.consensus_direction.value,
                consensus_probability=summary.consensus_probability,
                consensus_confidence=summary.consensus_confidence,
                long_weight=summary.long_weight,
                short_weight=summary.short_weight,
                neutral_weight=summary.neutral_weight,
                dissenters_json=list(summary.dissenters),
                dissent_probability=summary.dissent_probability,
                dissent_confidence=summary.dissent_confidence,
                challenge_blocked=summary.challenge_blocked,
                expected_gain=summary.expected_gain,
                expected_loss=summary.expected_loss,
                gross_ev=summary.gross_ev,
                funding_carry=summary.funding_carry,
                estimated_cost=summary.estimated_cost,
                net_ev=summary.net_ev,
                benchmark_return=summary.benchmark_return,
                alpha_estimate=summary.alpha_estimate,
                consensus_snapshot_json=_json_object(summary.consensus_snapshot),
                adversarial_snapshot_json=_json_object(summary.adversarial_snapshot),
                ev_snapshot_json=_json_object(summary.ev_snapshot),
                cost_snapshot_json=_json_object(summary.cost_snapshot),
            )
        )
        for gate in bundle.gates:
            self._session.add(
                GateEvaluationModel(
                    id=gate.id,
                    decision_cycle_id=gate.decision_cycle_id,
                    gate_type=gate.gate_type.value,
                    sequence=gate.sequence,
                    passed=gate.passed,
                    reason_code=gate.reason_code.value,
                    input_json=_json_object(gate.input),
                    output_json=_json_object(gate.output),
                    evaluated_at=gate.evaluated_at,
                    duration_ms=gate.duration_ms,
                )
            )
        proposal = bundle.proposal
        if proposal is not None:
            self._session.add(
                TradeProposalModel(
                    id=proposal.id,
                    decision_cycle_id=proposal.decision_cycle_id,
                    experiment_id=proposal.experiment_id,
                    symbol=proposal.symbol,
                    direction=proposal.direction.value,
                    status=proposal.status.value,
                    reason_code=proposal.reason_code.value,
                    proposed_at=proposal.proposed_at,
                    expires_at=proposal.expires_at,
                    entry_policy_json=_json_object(proposal.entry_policy),
                    exit_policy_json=_json_object(proposal.exit_policy),
                    sizing_snapshot_json=_json_object(proposal.sizing_snapshot),
                    approved_quantity=proposal.approved_quantity,
                    approved_notional=proposal.approved_notional,
                    risk_at_stop=proposal.risk_at_stop,
                )
            )
        await self._session.flush()

        if frame_created:
            await self._events.append(
                frame.id,
                "market_frame",
                0,
                (
                    NewDomainEvent(
                        aggregate_id=frame.id,
                        aggregate_type="market_frame",
                        event_type="market_frame.accepted",
                        payload=_event_payload(record_to_dict(frame)),
                        metadata={"experiment_id": str(frame.experiment_id)},
                        occurred_at=frame.observed_at,
                    ),
                ),
            )

        bundle_data = bundle.to_dict()
        agent_data = cast(list[object], bundle_data["agents"])
        gate_data = cast(list[object], bundle_data["gates"])
        decision_events: list[NewDomainEvent] = [
            NewDomainEvent(
                aggregate_id=cycle.id,
                aggregate_type="decision_cycle",
                event_type="decision_cycle.completed",
                payload=_event_payload(record_to_dict(cycle)),
                metadata={"bundle_hash": bundle_hash},
                occurred_at=cycle.completed_at,
            )
        ]
        for agent, payload in zip(bundle.agents, agent_data, strict=True):
            decision_events.append(
                NewDomainEvent(
                    aggregate_id=cycle.id,
                    aggregate_type="decision_cycle",
                    event_type="agent_evaluation.recorded",
                    payload=_event_payload(payload),
                    metadata={"agent_name": agent.agent_name},
                    occurred_at=agent.created_at,
                )
            )
        for gate, payload in zip(bundle.gates, gate_data, strict=True):
            decision_events.append(
                NewDomainEvent(
                    aggregate_id=cycle.id,
                    aggregate_type="decision_cycle",
                    event_type=("gate.passed" if gate.passed else "gate.failed"),
                    payload=_event_payload(payload),
                    metadata={"gate_type": gate.gate_type.value},
                    occurred_at=gate.evaluated_at,
                )
            )
        if proposal is not None:
            event_suffix = "approved" if proposal.status is ProposalStatus.APPROVED else "rejected"
            decision_events.append(
                NewDomainEvent(
                    aggregate_id=cycle.id,
                    aggregate_type="decision_cycle",
                    event_type=f"proposal.{event_suffix}",
                    payload=_event_payload(record_to_dict(proposal)),
                    metadata={"proposal_id": str(proposal.id)},
                    occurred_at=proposal.proposed_at,
                )
            )
        await self._events.append(
            cycle.id,
            "decision_cycle",
            0,
            tuple(decision_events),
        )
        return DecisionRecordResult(True, cycle.id, bundle_hash)

    async def get_bundle(self, decision_cycle_id: UUID) -> DecisionBundleView:
        cycle = await self._session.get(DecisionCycleModel, decision_cycle_id)
        if cycle is None:
            raise LookupError(f"decision cycle not found: {decision_cycle_id}")
        frame = await self._session.get(MarketFrameModel, cycle.market_frame_id)
        summary = await self._session.get(DecisionSummaryModel, decision_cycle_id)
        experiment = await self._session.get(ExperimentModel, cycle.experiment_id)
        if frame is None or summary is None or experiment is None:
            raise IncompleteDecisionBundleError("decision parent projections are incomplete")
        agent_rows = (
            await self._session.execute(
                select(AgentEvaluationModel, AgentVersionModel.agent_name)
                .join(
                    AgentVersionModel, AgentVersionModel.id == AgentEvaluationModel.agent_version_id
                )
                .where(AgentEvaluationModel.decision_cycle_id == decision_cycle_id)
            )
        ).all()
        by_name = {name: row for row, name in agent_rows}
        gate_rows = (
            await self._session.scalars(
                select(GateEvaluationModel)
                .where(GateEvaluationModel.decision_cycle_id == decision_cycle_id)
                .order_by(GateEvaluationModel.sequence)
            )
        ).all()
        proposal = await self._session.scalar(
            select(TradeProposalModel).where(
                TradeProposalModel.decision_cycle_id == decision_cycle_id
            )
        )
        if set(by_name) != set(ALL_AGENTS):
            raise IncompleteDecisionBundleError("decision does not contain all configured agents")

        bundle = DecisionBundle(
            market_frame=MarketFrameRecord(
                id=frame.id,
                experiment_id=frame.experiment_id,
                symbol=frame.symbol,
                venue=frame.venue,
                timeframe=frame.timeframe,
                bar_open_at=frame.bar_open_at,
                bar_close_at=frame.bar_close_at,
                observed_at=frame.observed_at,
                open=frame.open,
                high=frame.high,
                low=frame.low,
                close=frame.close,
                volume=frame.volume,
                best_bid=frame.best_bid,
                best_ask=frame.best_ask,
                mark_price=frame.mark_price,
                index_price=frame.index_price,
                funding_rate=frame.funding_rate,
                primary_spot_price=frame.primary_spot_price,
                secondary_venue_price=frame.secondary_venue_price,
                bar_snapshot=_event_payload(frame.bar_snapshot_json),
                orderbook_snapshot=_event_payload(frame.orderbook_snapshot_json),
                source_manifest=_event_payload(frame.source_manifest_json),
                source_sequence=_event_payload(frame.source_sequence_json),
                quality_status=QualityStatus(frame.quality_status),
                quality_results=_event_payload(frame.quality_results_json),
                content_hash=frame.content_hash,
            ),
            cycle=DecisionCycleRecord(
                id=cycle.id,
                experiment_id=cycle.experiment_id,
                market_frame_id=cycle.market_frame_id,
                strategy_version_id=cycle.strategy_version_id,
                symbol=cycle.symbol,
                timeframe=cycle.timeframe,
                cycle_at=cycle.cycle_at,
                regime=cycle.regime,
                feature_snapshot=_event_payload(cycle.feature_snapshot_json),
                feature_version=cycle.feature_version,
                status=DecisionStatus(cycle.status),
                direction=Direction(cycle.direction),
                disposition=Disposition(cycle.disposition),
                reason_code=ReasonCode(cycle.reason_code),
                created_at=cycle.created_at,
                completed_at=cycle.completed_at,
            ),
            agents=tuple(
                AgentEvaluationRecord(
                    id=by_name[name].id,
                    decision_cycle_id=by_name[name].decision_cycle_id,
                    agent_version_id=by_name[name].agent_version_id,
                    agent_name=name,
                    compatible=by_name[name].compatible,
                    enabled=by_name[name].enabled,
                    weight=Decimal(by_name[name].weight),
                    direction=Direction(by_name[name].direction),
                    probability=Decimal(by_name[name].probability),
                    confidence=Decimal(by_name[name].confidence),
                    risk=Decimal(by_name[name].risk),
                    input_snapshot=_event_payload(by_name[name].input_snapshot_json),
                    reason_codes=tuple(
                        ReasonCode(reason) for reason in by_name[name].reason_codes_json
                    ),
                    explanation=_event_payload(by_name[name].explanation_json),
                    duration_ms=by_name[name].duration_ms,
                    created_at=by_name[name].created_at,
                )
                for name in ALL_AGENTS
            ),
            summary=DecisionSummaryRecord(
                decision_cycle_id=summary.decision_cycle_id,
                consensus_direction=Direction(summary.consensus_direction),
                consensus_probability=summary.consensus_probability,
                consensus_confidence=summary.consensus_confidence,
                long_weight=summary.long_weight,
                short_weight=summary.short_weight,
                neutral_weight=summary.neutral_weight,
                dissenters=tuple(summary.dissenters_json),
                dissent_probability=summary.dissent_probability,
                dissent_confidence=summary.dissent_confidence,
                challenge_blocked=summary.challenge_blocked,
                expected_gain=summary.expected_gain,
                expected_loss=summary.expected_loss,
                gross_ev=summary.gross_ev,
                funding_carry=summary.funding_carry,
                estimated_cost=summary.estimated_cost,
                net_ev=summary.net_ev,
                benchmark_return=summary.benchmark_return,
                alpha_estimate=summary.alpha_estimate,
                consensus_snapshot=_event_payload(summary.consensus_snapshot_json),
                adversarial_snapshot=_event_payload(summary.adversarial_snapshot_json),
                ev_snapshot=_event_payload(summary.ev_snapshot_json),
                cost_snapshot=_event_payload(summary.cost_snapshot_json),
            ),
            gates=tuple(
                GateEvaluationRecord(
                    id=gate.id,
                    decision_cycle_id=gate.decision_cycle_id,
                    gate_type=GateType(gate.gate_type),
                    sequence=gate.sequence,
                    passed=gate.passed,
                    reason_code=ReasonCode(gate.reason_code),
                    input=_event_payload(gate.input_json),
                    output=_event_payload(gate.output_json),
                    evaluated_at=gate.evaluated_at,
                    duration_ms=gate.duration_ms,
                )
                for gate in gate_rows
            ),
            proposal=(
                TradeProposalRecord(
                    id=proposal.id,
                    decision_cycle_id=proposal.decision_cycle_id,
                    experiment_id=proposal.experiment_id,
                    symbol=proposal.symbol,
                    direction=Direction(proposal.direction),
                    status=ProposalStatus(proposal.status),
                    reason_code=ReasonCode(proposal.reason_code),
                    proposed_at=proposal.proposed_at,
                    expires_at=proposal.expires_at,
                    entry_policy=_event_payload(proposal.entry_policy_json),
                    exit_policy=_event_payload(proposal.exit_policy_json),
                    sizing_snapshot=_event_payload(proposal.sizing_snapshot_json),
                    approved_quantity=proposal.approved_quantity,
                    approved_notional=proposal.approved_notional,
                    risk_at_stop=proposal.risk_at_stop,
                )
                if proposal is not None
                else None
            ),
        )
        try:
            bundle.validate()
        except (TypeError, ValueError) as exc:
            raise IncompleteDecisionBundleError(str(exc)) from exc
        events = await self._events.load_stream(decision_cycle_id, "decision_cycle")
        return DecisionBundleView(
            bundle=bundle,
            events=events,
            config_hash=experiment.config_hash,
            manifest_hash=experiment.manifest_hash,
        )
