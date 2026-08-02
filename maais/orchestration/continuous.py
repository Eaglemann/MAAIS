"""Serialized continuous paper-position observation and protection."""

from __future__ import annotations

from collections.abc import Mapping

from maais.db.unit_of_work import UnitOfWork
from maais.execution.paper.exits import ExitPlanStatus
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.events import (
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
)
from maais.orchestration.observations import EligibleBookTimeout, MarketObservationBuffer
from maais.orchestration.protection import (
    PositionProtectionService,
    ProtectionContext,
)


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

    async def _observe_mark(self, event: ObservedMarketEvent) -> None:
        payload = event.payload
        if not isinstance(payload, MarkFundingPayload):
            raise TypeError("continuous mark payload is invalid")
        if event.symbol not in self._manifest.symbols:
            raise ContinuousRuntimeConflict("mark symbol is outside the experiment")

        async with self._uow.begin() as transaction:
            account = await transaction.paper_execution.load_account(self._manifest.experiment_id)
            exit_plans = await transaction.paper_execution.load_open_exit_plans(
                self._manifest.experiment_id
            )
            control = await transaction.controls.current(self._manifest.experiment_id)
            position = account.positions.get(event.symbol)
            if position is None or position.is_flat:
                return
            matching_plans = tuple(
                plan for plan in exit_plans if plan.position_id == position.position_id
            )
            if len(matching_plans) != 1:
                raise ContinuousRuntimeConflict(
                    "open paper position requires exactly one protective exit plan"
                )
            exit_plan = matching_plans[0]
            entry_proposal_id = await transaction.paper_execution.load_position_entry_proposal_id(
                self._manifest.experiment_id,
                position.position_id,
            )

        prior_books = self._observations.books_at_or_before(event.symbol, event.observed_at)
        triggers = exit_plan.status is ExitPlanStatus.TRIGGERED
        if not triggers:
            triggers = (
                exit_plan.evaluate_mark(payload.mark_price, event.observed_at).intent is not None
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

        outcome = self._protection.evaluate_mark(
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
                entry_admission_halted=control.kill_switch_active,
            ),
        )
        async with self._uow.begin() as transaction:
            await transaction.orchestration.record_protection_outcome(
                outcome,
                manifest=self._manifest,
            )
