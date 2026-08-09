"""Production composition root for one keyless live paper application."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from maais.db.recovery_store import PostgresRecoveryStateStore
from maais.db.unit_of_work import UnitOfWork
from maais.execution.paper.authorization import ExecutionAuthorizer
from maais.execution.paper.broker import PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.market_data.events import MarketEventKind, ObservedMarketEvent
from maais.market_data.history import CausalFrameHistory
from maais.market_data.recovery import MarketCursor
from maais.monitoring.admission import OfficialAdmissionPolicy
from maais.orchestration.bootstrap import LivePaperRuntimeSnapshot
from maais.orchestration.context import (
    LiveEntryContextAssembler,
    PersistentTradingControls,
)
from maais.orchestration.continuous import ContinuousPaperObserver
from maais.orchestration.flatten import (
    LivePaperFlattenPlanner,
    PostgresFlattenSourceLoader,
)
from maais.orchestration.observations import (
    MarketObservationBuffer,
    RuntimeHealthRegistry,
)
from maais.orchestration.operator_control import OperatorCommandExecutor
from maais.orchestration.protection import PositionProtectionService
from maais.orchestration.recovery import GapRecoveryManager
from maais.orchestration.runtime import AtomicCycleDispatcher
from maais.orchestration.service import OfficialOrchestrationService
from maais.orchestration.supervisor import (
    PaperWorkerSupervisor,
    PublicDataPort,
)
from maais.orchestration.worker import (
    ClosedBarDispatchEngine,
    CursorKey,
    DispatchResult,
)

Sleep = Callable[[float], Awaitable[None]]


class RuntimeAssemblyError(RuntimeError):
    pass


class FuturesPreflightResult(Protocol):
    @property
    def exchange_filters(self) -> tuple[ExchangeFilterSnapshot, ...]: ...


class FuturesRuntimePort(Protocol):
    async def preflight(self, required_symbols: Sequence[str]) -> FuturesPreflightResult: ...

    async def get_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> tuple[ObservedMarketEvent, ...]: ...


class HealthAwareDispatchEngine:
    """Publish current component liveness at the exact closed-bar cutoff."""

    def __init__(
        self,
        engine: ClosedBarDispatchEngine,
        health: RuntimeHealthRegistry,
        mandatory_components: tuple[str, ...],
    ) -> None:
        self._engine = engine
        self._health = health
        self._mandatory_components = mandatory_components

    @property
    def cursors(self) -> Mapping[CursorKey, MarketCursor]:
        return self._engine.cursors

    async def process(self, event: ObservedMarketEvent) -> DispatchResult:
        if event.kind is not MarketEventKind.CLOSED_BAR:
            return await self._engine.process(event)
        for component in self._mandatory_components:
            self._health.heartbeat(component, event.observed_at)
        try:
            return await self._engine.process(event)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            for component in self._mandatory_components:
                self._health.failure(component, detail, event.observed_at)
            raise


@dataclass(frozen=True, slots=True)
class LivePaperApplication:
    snapshot: LivePaperRuntimeSnapshot
    exchange_filters: Mapping[str, ExchangeFilterSnapshot]
    current_filter_rules_hashes: Mapping[str, str]
    history: CausalFrameHistory
    observations: MarketObservationBuffer
    health: RuntimeHealthRegistry
    engine: HealthAwareDispatchEngine
    operator_commands: OperatorCommandExecutor
    supervisor: PaperWorkerSupervisor

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "exchange_filters",
            MappingProxyType(dict(self.exchange_filters)),
        )
        object.__setattr__(
            self,
            "current_filter_rules_hashes",
            MappingProxyType(dict(self.current_filter_rules_hashes)),
        )


async def assemble_live_paper_application(
    *,
    uow: UnitOfWork,
    snapshot: LivePaperRuntimeSnapshot,
    worker_id: UUID,
    platform_run_id: UUID | None = None,
    futures_rest: FuturesRuntimePort,
    public_data: PublicDataPort,
    signing_key: bytes,
    now: Callable[[], datetime] | None = None,
    sleep: Sleep = asyncio.sleep,
) -> LivePaperApplication:
    observed_now = now or (lambda: datetime.now(timezone.utc))
    current_preflight = await futures_rest.preflight(snapshot.manifest.symbols)
    current_filters = _current_filters(snapshot, current_preflight.exchange_filters)
    pinned_filters = dict(snapshot.policy.exchange_filters)

    history = CausalFrameHistory(
        snapshot.manifest.experiment_id,
        snapshot.manifest.symbols,
        maximum_bars=snapshot.policy.history_bars,
    )
    history.restore(snapshot.history)
    observations = MarketObservationBuffer(snapshot.manifest.symbols)
    admission_policy = OfficialAdmissionPolicy.conservative()
    health = RuntimeHealthRegistry(admission_policy.mandatory_components)
    market_fills = MarketFillEngine(snapshot.policy.integrity_policy().max_book_age)
    authorizer = ExecutionAuthorizer(signing_key)
    broker = PaperBroker(
        clock=DeterministicClock(observed_now),
        authorizer=authorizer,
        market_fills=market_fills,
    )
    contexts = LiveEntryContextAssembler(
        uow=uow,
        manifest=snapshot.manifest,
        policy=snapshot.policy,
        history=history,
        observations=observations,
        health=health,
        controls=PersistentTradingControls(uow),
        exchange_filters=pinned_filters,
    )
    service = OfficialOrchestrationService(
        history,
        authorizer=authorizer,
        paper_broker=broker,
    )
    dispatcher = AtomicCycleDispatcher(
        uow=uow,
        manifest=snapshot.manifest,
        strategy_version_id=snapshot.strategy_version_id,
        agent_version_ids=dict(snapshot.agent_version_ids),
        history=history,
        entry_contexts=contexts,
        integrity_policy=snapshot.policy.integrity_policy(),
        service=service,
    )
    protection = PositionProtectionService(broker)
    continuous = ContinuousPaperObserver(
        uow=uow,
        manifest=snapshot.manifest,
        policy=snapshot.policy,
        observations=observations,
        protection=protection,
        market_fills=market_fills,
        exchange_filters=pinned_filters,
    )
    recovery = GapRecoveryManager(
        backfill=futures_rest,
        store=PostgresRecoveryStateStore(uow),
        now=observed_now,
        sleep=sleep,
    )
    cursor_map = {
        (cursor.venue, cursor.stream, cursor.symbol, cursor.timeframe): cursor
        for cursor in snapshot.cursors
    }
    serialized = ClosedBarDispatchEngine(
        experiment_id=snapshot.manifest.experiment_id,
        symbols=snapshot.manifest.symbols,
        dispatcher=dispatcher,
        recovery=recovery,
        cursors=cursor_map,
        continuous=continuous,
    )
    engine = HealthAwareDispatchEngine(
        serialized,
        health,
        admission_policy.mandatory_components,
    )
    operator_commands = OperatorCommandExecutor(
        uow=uow,
        manifest=snapshot.manifest,
        worker_id=worker_id,
        platform_run_id=platform_run_id,
        now=observed_now,
        flatten_planner=LivePaperFlattenPlanner(
            manifest=snapshot.manifest,
            policy=snapshot.policy,
            source_loader=PostgresFlattenSourceLoader(uow),
            observations=observations,
            broker=broker,
            exchange_filters=pinned_filters,
            now=observed_now,
        ),
    )
    supervisor = PaperWorkerSupervisor(
        uow=uow,
        manifest=snapshot.manifest,
        worker_id=worker_id,
        public_data=public_data,
        observations=observations,
        engine=engine,
        operator_commands=operator_commands,
        now=observed_now,
        sleep=sleep,
    )
    return LivePaperApplication(
        snapshot=snapshot,
        exchange_filters=pinned_filters,
        current_filter_rules_hashes={
            symbol: current.rules_hash for symbol, current in current_filters.items()
        },
        history=history,
        observations=observations,
        health=health,
        engine=engine,
        operator_commands=operator_commands,
        supervisor=supervisor,
    )


def _current_filters(
    snapshot: LivePaperRuntimeSnapshot,
    filters: tuple[ExchangeFilterSnapshot, ...],
) -> dict[str, ExchangeFilterSnapshot]:
    current = {item.symbol: item for item in filters}
    if len(current) != len(filters) or set(current) != set(snapshot.manifest.symbols):
        raise RuntimeAssemblyError("current exchange filters do not cover exact manifest symbols")
    changed = sorted(
        symbol
        for symbol, pinned in snapshot.policy.exchange_filters.items()
        if current[symbol].rules_hash != pinned.rules_hash
    )
    if changed:
        raise RuntimeAssemblyError(
            "current exchange rules changed from the pinned manifest: " + ", ".join(changed)
        )
    return current
