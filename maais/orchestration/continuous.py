"""Serialized continuous paper-position observation and protection."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import Direction, PaperOrderSide
from maais.execution.paper.exits import ExitPlanStatus
from maais.execution.paper.fills import FillRejection, MarketFillEngine, MarketFillRequest
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import (
    FundingSettlementPayload,
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
    OrderBookPayload,
)
from maais.orchestration.observations import EligibleBookTimeout, MarketObservationBuffer
from maais.orchestration.protection import (
    FundingSettlementCommand,
    PositionProtectionService,
    ProtectionContext,
)
from maais.research.counterfactuals import CounterfactualStatus


class ContinuousRuntimeConflict(RuntimeError):
    pass


class ContinuousPaperObserver:
    """Advance official open positions for every serialized mark observation."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        manifest: ExperimentManifest,
        policy: LivePaperPolicy,
        observations: MarketObservationBuffer,
        protection: PositionProtectionService,
        market_fills: MarketFillEngine,
        exchange_filters: Mapping[str, ExchangeFilterSnapshot],
    ) -> None:
        filters = dict(exchange_filters)
        if set(filters) != set(manifest.symbols) or any(
            symbol != snapshot.symbol for symbol, snapshot in filters.items()
        ):
            raise ValueError("continuous exchange filters must cover exact manifest symbols")
        mismatched = tuple(
            symbol
            for symbol, snapshot in filters.items()
            if snapshot.content_hash != policy.exchange_filter_hashes[symbol]
        )
        if mismatched:
            raise ValueError(
                "continuous exchange filters differ from manifest snapshots: "
                + ", ".join(sorted(mismatched))
            )
        self._uow = uow
        self._manifest = manifest
        self._policy = policy
        self._observations = observations
        self._protection = protection
        self._market_fills = market_fills
        self._filters = filters

    async def observe(
        self,
        event: ObservedMarketEvent,
        *,
        context_events: tuple[ObservedMarketEvent, ...],
    ) -> None:
        del context_events
        if event.kind is MarketEventKind.MARK_FUNDING:
            await self._observe_mark(event)
        elif event.kind is MarketEventKind.FUNDING_SETTLEMENT:
            await self._observe_funding(event)
        elif event.kind is MarketEventKind.ORDER_BOOK:
            await self._observe_book(event)

    async def _observe_book(self, event: ObservedMarketEvent) -> None:
        if not isinstance(event.payload, OrderBookPayload):
            raise TypeError("continuous book payload is invalid")
        if event.symbol not in self._manifest.symbols:
            raise ContinuousRuntimeConflict("book symbol is outside the experiment")
        book = next(
            (
                item
                for item in self._observations.books_at_or_before(
                    event.symbol,
                    event.observed_at,
                )
                if item.event_id == event.event_id
                and item.observed_at == event.observed_at
                and item.sequence == event.sequence
            ),
            None,
        )
        if book is None:
            return
        async with self._uow.begin() as transaction:
            states = await transaction.counterfactuals.get_unresolved(self._manifest.experiment_id)
            for state in states:
                if (
                    state.symbol != event.symbol
                    or state.status is not CounterfactualStatus.PENDING
                    or book.observed_at <= state.eligible_after
                ):
                    continue
                side = (
                    PaperOrderSide.BUY if state.direction is Direction.LONG else PaperOrderSide.SELL
                )
                try:
                    fill = self._market_fills.fill(
                        MarketFillRequest(
                            symbol=state.symbol,
                            side=side,
                            quantity=state.quantity,
                            eligible_after=state.eligible_after,
                            decision_executable_price=state.decision_executable_price,
                            taker_fee_rate=state.fee_rate,
                        ),
                        (book,),
                    )
                except FillRejection as exc:
                    updated = state.mark_no_fill(exc.reason, book.observed_at)
                else:
                    updated = state.enter(
                        fill,
                        plan_id=uuid5(
                            NAMESPACE_URL,
                            f"maais://counterfactual-exit-plan/{state.counterfactual_id}",
                        ),
                    )
                await transaction.counterfactuals.record(updated)

    async def _observe_funding(self, event: ObservedMarketEvent) -> None:
        payload = event.payload
        if not isinstance(payload, FundingSettlementPayload):
            raise TypeError("continuous funding payload is invalid")
        if event.symbol not in self._manifest.symbols:
            raise ContinuousRuntimeConflict("funding symbol is outside the experiment")
        async with self._uow.begin() as transaction:
            existing = await transaction.paper_execution.load_funding_event(
                self._manifest.experiment_id,
                event.event_id,
            )
            if existing is not None:
                if (
                    existing.funding_at != payload.funding_at
                    or existing.observed_at != event.observed_at
                    or existing.rate != payload.funding_rate
                    or existing.rate_type != payload.rate_type
                    or existing.mark_price != payload.mark_price
                ):
                    raise ContinuousRuntimeConflict(
                        "funding event identity has different persisted content"
                    )
            account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
            position = account.positions.get(event.symbol)
            if (
                existing is None
                and position is not None
                and not position.is_flat
                and position.opened_at is not None
                and payload.funding_at >= position.opened_at
            ):
                outcome = self._protection.apply_funding(
                    FundingSettlementCommand(
                        experiment_id=self._manifest.experiment_id,
                        symbol=event.symbol,
                        market_event_id=event.event_id,
                        funding_at=payload.funding_at,
                        observed_at=event.observed_at,
                        mark_price=payload.mark_price,
                        rate=payload.funding_rate,
                        rate_type=payload.rate_type,
                        account=account,
                    )
                )
                await transaction.orchestration.record_funding_outcome(outcome)
            states = await transaction.counterfactuals.get_unresolved(self._manifest.experiment_id)
            for state in states:
                if (
                    state.symbol == event.symbol
                    and state.status is CounterfactualStatus.OPEN
                    and state.entry_fill is not None
                    and payload.funding_at >= state.entry_fill.fill_at
                ):
                    updated = state.apply_funding(
                        payload.funding_rate,
                        payload.mark_price,
                        event.observed_at,
                        market_event_id=event.event_id,
                    )
                    await transaction.counterfactuals.record(updated)

    async def _observe_mark(self, event: ObservedMarketEvent) -> None:
        payload = event.payload
        if not isinstance(payload, MarkFundingPayload):
            raise TypeError("continuous mark payload is invalid")
        if event.symbol not in self._manifest.symbols:
            raise ContinuousRuntimeConflict("mark symbol is outside the experiment")

        official_context = None
        async with self._uow.begin() as transaction:
            account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
            states = await transaction.counterfactuals.get_unresolved(self._manifest.experiment_id)
            position = account.positions.get(event.symbol)
            if position is not None and not position.is_flat:
                exit_plans = await transaction.paper_execution.load_open_exit_plans(
                    self._manifest.experiment_id
                )
                matching_plans = tuple(
                    plan for plan in exit_plans if plan.position_id == position.position_id
                )
                if len(matching_plans) != 1:
                    raise ContinuousRuntimeConflict(
                        "open paper position requires exactly one protective exit plan"
                    )
                control = await transaction.controls.current(self._manifest.experiment_id)
                entry_proposal_id = (
                    await transaction.paper_execution.load_position_entry_proposal_id(
                        self._manifest.experiment_id,
                        position.position_id,
                    )
                )
                official_context = (
                    account,
                    matching_plans[0],
                    entry_proposal_id,
                    control.kill_switch_active,
                )

        counterfactual_updates = tuple(
            state.observe_mark(
                payload.mark_price,
                event.observed_at,
                market_event_id=event.event_id,
            )
            for state in states
            if state.symbol == event.symbol and state.status is CounterfactualStatus.OPEN
        )
        protection_outcome = None
        if official_context is not None:
            account, exit_plan, entry_proposal_id, entry_admission_halted = official_context
            prior_books = self._observations.books_at_or_before(event.symbol, event.observed_at)
            triggers = exit_plan.status is ExitPlanStatus.TRIGGERED
            if not triggers:
                triggers = (
                    exit_plan.evaluate_mark(payload.mark_price, event.observed_at).intent
                    is not None
                )
            future_books = ()
            if triggers:
                try:
                    future_books = await self._observations.books_after(
                        event.symbol,
                        event.observed_at + self._policy.execution_latency,
                        timeout=self._policy.book_wait_timeout,
                    )
                except EligibleBookTimeout:
                    future_books = ()
            protection_outcome = self._protection.evaluate_mark(
                event,
                ProtectionContext(
                    experiment_id=self._manifest.experiment_id,
                    entry_proposal_id=entry_proposal_id,
                    symbol=event.symbol,
                    account=account,
                    exit_plan=exit_plan,
                    exchange_filters=self._filters[event.symbol],
                    books=(*prior_books, *future_books),
                    execution_latency=self._policy.execution_latency,
                    order_ttl=self._policy.proposal_ttl,
                    taker_fee_rate=self._policy.taker_fee_rate,
                    entry_admission_halted=entry_admission_halted,
                ),
            )
        async with self._uow.begin() as transaction:
            if protection_outcome is not None:
                await transaction.orchestration.record_protection_outcome(
                    protection_outcome,
                    manifest=self._manifest,
                )
            for updated in counterfactual_updates:
                await transaction.counterfactuals.record(updated)
