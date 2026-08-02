"""Atomic production dispatch of one causal closed-bar decision cycle."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import ProposalStatus
from maais.experiments.manifest import ExperimentManifest
from maais.market_data.events import ClosedBarPayload, MarketEventKind, ObservedMarketEvent
from maais.market_data.frames import CausalMinuteFrame, CausalMinuteFrameBuilder, FrameKey
from maais.market_data.history import CausalFrameHistory
from maais.market_data.integrity.state_machine import (
    FrameAdmission,
    IntegrityPolicy,
    MarketIntegrityStateMachine,
)
from maais.market_data.recovery import MarketCursor, RecoveryState
from maais.orchestration.commands import EntryDecisionContext, OrchestrationCommand
from maais.orchestration.results import OrchestrationOutcome
from maais.orchestration.service import OfficialOrchestrationService
from maais.research.counterfactuals import CounterfactualStatus


class EntryContextFactory(Protocol):
    async def build(
        self,
        frame: CausalMinuteFrame,
        *,
        evaluated_at: datetime,
        completed_at: datetime,
    ) -> EntryDecisionContext: ...


class OrchestrationProcessor(Protocol):
    async def process(self, command: OrchestrationCommand) -> OrchestrationOutcome: ...


class AtomicCycleDispatcher:
    """Build, decide, and persist outcome/cursor/recovery in one transaction."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        manifest: ExperimentManifest,
        strategy_version_id: UUID,
        agent_version_ids: dict[str, UUID],
        history: CausalFrameHistory,
        entry_contexts: EntryContextFactory,
        integrity_policy: IntegrityPolicy,
        service: OrchestrationProcessor | None = None,
        frame_builder: CausalMinuteFrameBuilder | None = None,
    ) -> None:
        if strategy_version_id.int == 0:
            raise ValueError("runtime strategy_version_id cannot be nil")
        if set(agent_version_ids) != {entry.agent_name for entry in manifest.agent_versions}:
            raise ValueError("runtime agent version identities differ from manifest")
        self._uow = uow
        self._manifest = manifest
        self._strategy_version_id = strategy_version_id
        self._agent_version_ids = dict(agent_version_ids)
        self._history = history
        self._entry_contexts = entry_contexts
        self._integrity_policy = integrity_policy
        self._integrity = MarketIntegrityStateMachine(integrity_policy)
        self._service = service or OfficialOrchestrationService(history)
        self._frame_builder = frame_builder or CausalMinuteFrameBuilder()

    async def dispatch(
        self,
        event: ObservedMarketEvent,
        *,
        context_events: tuple[ObservedMarketEvent, ...],
        target_cursor: MarketCursor,
        recovery_progress: RecoveryState | None,
    ) -> None:
        payload = event.payload
        if event.kind is not MarketEventKind.CLOSED_BAR or not isinstance(
            payload, ClosedBarPayload
        ):
            raise ValueError("cycle dispatcher requires a closed-bar event")
        self._validate_cursor(event, target_cursor)
        if recovery_progress is not None:
            if (
                recovery_progress.experiment_id != self._manifest.experiment_id
                or recovery_progress.dispatched_through_sequence != target_cursor.sequence
                or recovery_progress.dispatched_through_event_id != target_cursor.event_id
            ):
                raise ValueError("recovery progress does not match the target cursor")

        cutoff = event.observed_at
        if recovery_progress is not None:
            historical_cutoff = payload.bar_close_at + self._integrity_policy.max_decision_lag
            cutoff = min(event.observed_at, historical_cutoff)
        frame = self._frame_builder.build(
            FrameKey(
                experiment_id=self._manifest.experiment_id,
                strategy_version_id=self._strategy_version_id,
                symbol=event.symbol,
                timeframe=payload.timeframe,
                bar_close_at=payload.bar_close_at,
            ),
            event,
            context_events,
            decision_cutoff=cutoff,
        )
        evaluated_at = event.observed_at
        completed_at = evaluated_at
        integrity = self._integrity.evaluate(
            self._history.integrity_context(frame, evaluated_at=evaluated_at)
        )
        entry_context = None
        if integrity.admission is FrameAdmission.ADMITTED:
            entry_context = await self._entry_contexts.build(
                frame,
                evaluated_at=evaluated_at,
                completed_at=completed_at,
            )
        outcome = await self._service.process(
            OrchestrationCommand(
                frame=frame,
                integrity=integrity,
                manifest=self._manifest,
                agent_version_ids=self._agent_version_ids,
                evaluated_at=evaluated_at,
                completed_at=completed_at,
                entry_context=entry_context,
            )
        )
        async with self._uow.begin() as transaction:
            existing_counterfactuals = await transaction.counterfactuals.get_unresolved(
                self._manifest.experiment_id
            )
            await transaction.orchestration.record_outcome(
                outcome,
                integrity=integrity,
                required_checks=self._integrity_policy.required_checks,
                evaluated_at=evaluated_at,
                cursor=target_cursor,
            )
            proposal = outcome.bundle.proposal
            decision_approved = proposal is not None and proposal.status is ProposalStatus.APPROVED
            mark_price = frame.mark_price or frame.bar.close
            for state in existing_counterfactuals:
                if state.symbol != event.symbol or state.status is not CounterfactualStatus.OPEN:
                    continue
                updated = state.observe_closed_bar(
                    mark_price=mark_price,
                    decision_direction=outcome.bundle.cycle.direction,
                    decision_approved=decision_approved,
                    closed_at=payload.bar_close_at,
                    market_event_id=event.event_id,
                )
                await transaction.counterfactuals.record(updated)
            if recovery_progress is not None:
                await transaction.market_data.record_recovery(recovery_progress)
        self._history.commit(frame)

    def _validate_cursor(
        self,
        event: ObservedMarketEvent,
        target: MarketCursor,
    ) -> None:
        payload = event.payload
        assert isinstance(payload, ClosedBarPayload)
        if (
            target.experiment_id != self._manifest.experiment_id
            or target.venue != event.venue
            or target.stream != event.stream
            or target.symbol != event.symbol
            or target.timeframe != payload.timeframe
            or target.event_id != event.event_id
            or target.sequence != event.sequence
            or target.venue_event_at != event.venue_event_at
            or target.observed_at != event.observed_at
            or target.bar_close_at != payload.bar_close_at
        ):
            raise ValueError("target cursor does not represent the dispatched closed bar")
