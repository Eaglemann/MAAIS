from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maais.db.repositories.artifacts import ArtifactRepository
from maais.db.repositories.controls import TradingControlRepository
from maais.db.repositories.counterfactuals import CounterfactualRepository
from maais.db.repositories.decisions import DecisionRepository
from maais.db.repositories.events import EventRepository
from maais.db.repositories.execution import PaperExecutionRepository
from maais.db.repositories.experiments import ExperimentRepository
from maais.db.repositories.incidents import IncidentRepository
from maais.db.repositories.market_data import MarketDataRepository
from maais.db.repositories.observability import ObservabilityRepository
from maais.db.repositories.operator_commands import OperatorCommandRepository
from maais.db.repositories.orchestration import OrchestrationRepository
from maais.db.repositories.platform import PlatformRepository
from maais.db.repositories.scheduled_operations import ScheduledOperationRepository
from maais.db.repositories.sessions import OperatorSessionRepository
from maais.db.repositories.workers import WorkerLeaseRepository


@dataclass(slots=True)
class UnitOfWorkContext:
    session: AsyncSession
    events: EventRepository
    experiments: ExperimentRepository
    decisions: DecisionRepository
    counterfactuals: CounterfactualRepository
    paper_execution: PaperExecutionRepository
    market_data: MarketDataRepository
    incidents: IncidentRepository
    orchestration: OrchestrationRepository
    workers: WorkerLeaseRepository
    controls: TradingControlRepository
    commands: OperatorCommandRepository
    platform: PlatformRepository
    artifacts: ArtifactRepository
    scheduled_operations: ScheduledOperationRepository
    sessions: OperatorSessionRepository
    observability: ObservabilityRepository


class UnitOfWork:
    """Own one database transaction shared by events, projections, and outbox."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[UnitOfWorkContext]:
        async with self._session_factory() as session:
            async with session.begin():
                events = EventRepository(session)
                yield UnitOfWorkContext(
                    session=session,
                    events=events,
                    experiments=ExperimentRepository(session, events),
                    decisions=DecisionRepository(session, events),
                    counterfactuals=CounterfactualRepository(session, events),
                    paper_execution=PaperExecutionRepository(session, events),
                    market_data=MarketDataRepository(session, events),
                    incidents=IncidentRepository(session, events),
                    orchestration=OrchestrationRepository(session, events),
                    workers=WorkerLeaseRepository(session, events),
                    controls=TradingControlRepository(session, events),
                    commands=OperatorCommandRepository(session, events),
                    platform=PlatformRepository(session),
                    artifacts=ArtifactRepository(session),
                    scheduled_operations=ScheduledOperationRepository(session),
                    sessions=OperatorSessionRepository(session),
                    observability=ObservabilityRepository(session),
                )
