from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from maais.agents.base import BaseAgent
from maais.agents.runner import build_agent_registry
from maais.config.constants import ALL_AGENTS, AgentName, Regime
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    ReasonCode,
)
from maais.feature_pipeline.features import FeatureSet
from maais.market_data.integrity.state_machine import (
    FrameAdmission,
    IntegrityPolicy,
    MarketIntegrityStateMachine,
)
from maais.orchestration.commands import OrchestrationCommand
from maais.orchestration.results import OrchestrationDisposition
from maais.orchestration.service import OfficialOrchestrationService
from tests.unit.experiments.test_manifest import _manifest
from tests.unit.market_data.test_integrity_state_machine import _context, _frame


class _FeatureComputer:
    def __init__(self, features: FeatureSet) -> None:
        self.features = features
        self.calls = 0

    def compute(self, frame) -> FeatureSet:
        self.calls += 1
        return replace(
            self.features,
            symbol=frame.key.symbol,
            timeframe=frame.key.timeframe,
            timestamp=frame.bar.bar_close_at,
        )


class _ThrowingAgent(BaseAgent):
    name = AgentName.MOMENTUM
    compatible_regimes = (Regime.TRENDING,)

    async def analyze(self, features):
        raise RuntimeError("simulated mandatory-agent failure")


def _command(*, admitted: bool) -> OrchestrationCommand:
    frame = _frame()
    context = _context(frame)
    if not admitted:
        context = replace(
            context,
            historical_bar_count=0,
            recent_close_returns=(),
        )
    integrity = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(context)
    return OrchestrationCommand(
        frame=frame,
        integrity=integrity,
        manifest=_manifest(
            experiment_id=frame.key.experiment_id,
            schema_revision="0009",
        ),
        agent_version_ids={name: UUID(int=index + 100) for index, name in enumerate(ALL_AGENTS)},
        evaluated_at=context.evaluated_at,
        completed_at=context.evaluated_at,
    )


def _features() -> FeatureSet:
    return FeatureSet(
        symbol="BTCUSDT",
        timeframe="1m",
        timestamp=_frame().bar.bar_close_at,
        regime=Regime.TRENDING,
        ema_fast=101.0,
        ema_slow=100.0,
        ema_signal=1.0,
        atr=1.0,
        rolling_std=0.01,
        bid_ask_spread=0.001,
        book_imbalance=0.2,
        funding_rate=0.0001,
        annualized_funding=0.1095,
        funding_bias="long_heavy",
        zscore_mean=101.0,
    )


async def test_quarantine_creates_complete_visible_outcome_without_running_features() -> None:
    command = _command(admitted=False)
    computer = _FeatureComputer(_features())
    service = OfficialOrchestrationService(computer)

    first = await service.process(command)
    second = await service.process(command)

    assert command.integrity.admission is FrameAdmission.QUARANTINED
    assert computer.calls == 0
    assert first.disposition is OrchestrationDisposition.QUARANTINED
    assert first == second
    assert first.bundle.bundle_hash == second.bundle.bundle_hash
    assert first.bundle.cycle.status is DecisionStatus.QUARANTINED
    assert first.bundle.cycle.disposition is Disposition.NEUTRAL
    assert first.bundle.cycle.direction is Direction.NEUTRAL
    assert first.bundle.proposal is None
    assert tuple(item.agent_name for item in first.bundle.agents) == ALL_AGENTS
    assert all(item.direction is Direction.NEUTRAL for item in first.bundle.agents)
    assert all(ReasonCode.DATA_QUALITY_FAILED in item.reason_codes for item in first.bundle.agents)
    assert tuple(item.gate_type for item in first.bundle.gates) == (GateType.DATA_QUALITY,)
    assert not first.bundle.gates[0].passed
    assert first.incident is not None
    assert first.incident.reason_code == "market_frame_quarantined"
    first.bundle.validate()


async def test_mandatory_agent_failure_blocks_cycle_with_eight_rows_and_incident() -> None:
    command = _command(admitted=True)
    computer = _FeatureComputer(_features())
    registry = tuple(
        _ThrowingAgent() if agent.name == AgentName.MOMENTUM else agent
        for agent in build_agent_registry()
    )
    service = OfficialOrchestrationService(computer, agents=registry)

    outcome = await service.process(command)

    assert computer.calls == 1
    assert outcome.disposition is OrchestrationDisposition.HALTED
    assert outcome.bundle.cycle.status is DecisionStatus.REJECTED
    assert outcome.bundle.cycle.disposition is Disposition.NEUTRAL
    assert outcome.bundle.cycle.reason_code is ReasonCode.AGENT_FAILED
    assert outcome.bundle.proposal is None
    assert tuple(item.agent_name for item in outcome.bundle.agents) == ALL_AGENTS
    failed = next(item for item in outcome.bundle.agents if item.agent_name == AgentName.MOMENTUM)
    assert failed.direction is Direction.NEUTRAL
    assert failed.reason_codes == (ReasonCode.AGENT_FAILED,)
    assert "simulated mandatory-agent failure" in str(failed.explanation["failure_reason"])
    assert tuple(item.gate_type for item in outcome.bundle.gates) == (
        GateType.DATA_QUALITY,
        GateType.CONSENSUS,
    )
    assert outcome.bundle.gates[0].passed
    assert not outcome.bundle.gates[1].passed
    assert outcome.incident is not None
    assert outcome.incident.component == "agents"
    assert outcome.incident.requires_operator_review
    outcome.bundle.validate()


def test_command_rejects_mismatched_frame_assessment_and_incomplete_agent_ids() -> None:
    command = _command(admitted=True)

    with pytest.raises(ValueError, match="integrity assessment"):
        replace(command, integrity=replace(command.integrity, frame_id=UUID(int=999)))
    with pytest.raises(ValueError, match="agent version"):
        replace(command, agent_version_ids={AgentName.MOMENTUM: UUID(int=100)})
    with pytest.raises(ValueError, match="experiment"):
        replace(command, manifest=_manifest(experiment_id=UUID(int=999)))
