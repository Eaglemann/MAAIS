from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from maais.agents.base import AgentOutput, BaseAgent
from maais.agents.runner import build_agent_registry
from maais.config.constants import ALL_AGENTS, AgentName, Regime
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    PaperOrderType,
    ProposalStatus,
    ReasonCode,
)
from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import ExecutionAuthorizer
from maais.execution.paper.broker import PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.feature_pipeline.features import FeatureSet
from maais.market_data.integrity.state_machine import (
    FrameAdmission,
    IntegrityPolicy,
    MarketIntegrityStateMachine,
)
from maais.monitoring.admission import (
    BenchmarkObservation,
    HealthObservation,
    MonitoringAdmissionContext,
    OfficialAdmissionPolicy,
    RollingVolatilityBaseline,
)
from maais.orchestration.commands import EntryDecisionContext, OrchestrationCommand
from maais.orchestration.results import OrchestrationDisposition
from maais.orchestration.service import OfficialOrchestrationService
from maais.risk.official import DrawdownSnapshot, RiskCheck
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


class _FixedAgent(BaseAgent):
    compatible_regimes = ()

    def __init__(self, name: str, direction: Direction, probability: float = 0.9) -> None:
        self.name = name
        self._direction = direction
        self._probability = probability

    async def analyze(self, features):
        return AgentOutput(
            self.name,
            self._direction.value,
            self._probability,
            0.8 if self._direction is not Direction.NEUTRAL else 0.0,
            0.2,
        )


def _command(
    *,
    admitted: bool,
    entry_context: EntryDecisionContext | None = None,
) -> OrchestrationCommand:
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
        entry_context=entry_context,
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
        atr=2.5,
        rolling_std=0.01,
        bid_ask_spread=0.001,
        book_imbalance=0.2,
        funding_rate=0.0001,
        annualized_funding=0.1095,
        funding_bias="long_heavy",
        zscore_mean=101.0,
    )


def _entry_context(*, kill_switch: bool = False) -> EntryDecisionContext:
    command = _command(admitted=True)
    evaluated_at = command.evaluated_at
    policy = OfficialAdmissionPolicy.conservative()
    account = AccountState.create(command.manifest.experiment_id, Decimal("10000"), "USDT")
    health = tuple(
        HealthObservation(
            component,
            True,
            evaluated_at - timedelta(milliseconds=100),
            None,
        )
        for component in policy.mandatory_components
    )
    monitoring = MonitoringAdmissionContext(
        symbol="BTCUSDT",
        timeframe="1m",
        evaluated_at=evaluated_at,
        kill_switch_active=kill_switch,
        kill_switch_reason="operator_test" if kill_switch else None,
        kill_switch_version=1 if kill_switch else 0,
        kill_switch_changed_at=evaluated_at - timedelta(milliseconds=100),
        kill_switch_changed_by="test_operator" if kill_switch else "system",
        health=health,
        volatility=RollingVolatilityBaseline(
            symbol="BTCUSDT",
            timeframe="1m",
            sample_count=60,
            baseline_std=Decimal("0.002"),
            current_std=Decimal("0.0025"),
            spread_fraction=Decimal("0.001"),
            observed_at=evaluated_at - timedelta(milliseconds=100),
        ),
        benchmark=BenchmarkObservation(
            symbol="BTCUSDT",
            return_fraction=Decimal("0"),
            observed_at=evaluated_at - timedelta(milliseconds=100),
            source_event_id="benchmark-explicit-zero",
        ),
    )
    filters = ExchangeFilterSnapshot(
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("200"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.MARKET,),
        captured_at=command.frame.cutoff_at,
    )
    book_at = evaluated_at + timedelta(milliseconds=101)
    book = BookSnapshot(
        event_id="eligible-book",
        symbol="BTCUSDT",
        venue_event_at=book_at - timedelta(milliseconds=1),
        observed_at=book_at,
        sequence=200,
        bids=(BookLevel(Decimal("100.4"), Decimal("200")),),
        asks=(BookLevel(Decimal("100.5"), Decimal("200")),),
        mark_price=Decimal("100.45"),
    )
    return EntryDecisionContext(
        monitoring=monitoring,
        drawdown=DrawdownSnapshot(account.peak_equity, account.equity),
        open_positions=(),
        correlations=(),
        exchange_filters=filters,
        account=account,
        books=(book,),
        active_exit_plan=None,
        proposal_ttl=timedelta(seconds=30),
        execution_latency=timedelta(milliseconds=100),
        taker_fee_rate=Decimal("0.0005"),
    )


def _fixed_registry(direction: Direction) -> tuple[BaseAgent, ...]:
    return tuple(_FixedAgent(name, direction) for name in ALL_AGENTS)


def _execution_service(
    computer: _FeatureComputer,
    direction: Direction,
) -> OfficialOrchestrationService:
    key = b"orchestration test signing key of at least 32 bytes"
    authorizer = ExecutionAuthorizer(key)
    broker = PaperBroker(
        clock=DeterministicClock(lambda: _command(admitted=True).completed_at),
        authorizer=authorizer,
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )
    return OfficialOrchestrationService(
        computer,
        agents=_fixed_registry(direction),
        authorizer=authorizer,
        paper_broker=broker,
    )


async def test_expected_warmup_creates_complete_visible_outcome_without_an_incident() -> None:
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
    assert first.bundle.market_frame.index_price == command.frame.index_price
    assert first.bundle.market_frame.primary_spot_price == command.frame.primary_spot_price
    assert first.bundle.market_frame.secondary_venue_price == command.frame.secondary_venue_price
    assert first.bundle.market_frame.bar_snapshot["trade_count"] == 50
    assert set(first.bundle.market_frame.source_manifest) == set(command.frame.source_manifest)
    assert first.bundle.market_frame.orderbook_snapshot["bids"]
    assert tuple(item.agent_name for item in first.bundle.agents) == ALL_AGENTS
    assert all(item.direction is Direction.NEUTRAL for item in first.bundle.agents)
    assert first.bundle.cycle.reason_code is ReasonCode.INSUFFICIENT_HISTORY
    assert all(ReasonCode.INSUFFICIENT_HISTORY in item.reason_codes for item in first.bundle.agents)
    assert tuple(item.gate_type for item in first.bundle.gates) == (GateType.DATA_QUALITY,)
    assert not first.bundle.gates[0].passed
    assert first.incident is None
    first.bundle.validate()


async def test_real_quality_failure_during_warmup_creates_operator_incident() -> None:
    command = _command(admitted=True)
    policy = replace(
        IntegrityPolicy.official(),
        max_venue_timestamp_skew=timedelta(microseconds=1),
    )
    integrity = MarketIntegrityStateMachine(policy).evaluate(
        replace(
            _context(command.frame),
            historical_bar_count=0,
            recent_close_returns=(),
        )
    )
    service = OfficialOrchestrationService(_FeatureComputer(_features()))

    outcome = await service.process(replace(command, integrity=integrity, entry_context=None))

    assert outcome.disposition is OrchestrationDisposition.QUARANTINED
    assert outcome.bundle.cycle.reason_code is ReasonCode.DATA_QUALITY_FAILED
    assert outcome.incident is not None
    assert outcome.incident.reason_code == "market_frame_quarantined"


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
    with pytest.raises(ValueError, match="taker fee"):
        replace(
            command,
            manifest=replace(
                command.manifest,
                fee_policy={"maker": "0.0002", "taker": "0.0004"},
            ),
            entry_context=_entry_context(),
        )


async def test_admitted_neutral_cycle_has_all_gates_and_no_synthetic_proposal() -> None:
    command = _command(admitted=True)
    service = _execution_service(_FeatureComputer(_features()), Direction.NEUTRAL)

    outcome = await service.process(command)

    assert outcome.disposition is OrchestrationDisposition.NEUTRAL
    assert outcome.bundle.cycle.status is DecisionStatus.COMPLETED
    assert outcome.bundle.cycle.disposition is Disposition.NEUTRAL
    assert outcome.bundle.proposal is None
    assert outcome.incident is None
    assert len(outcome.bundle.gates) == len(GateType)
    assert outcome.bundle.gates[0].passed
    assert not outcome.bundle.gates[2].passed
    assert all(not gate.passed for gate in outcome.bundle.gates[2:])


async def test_directional_monitoring_rejection_creates_isolated_counterfactual() -> None:
    entry_context = _entry_context(kill_switch=True)
    command = _command(admitted=True, entry_context=entry_context)
    service = _execution_service(_FeatureComputer(_features()), Direction.LONG)

    outcome = await service.process(command)

    assert outcome.disposition is OrchestrationDisposition.REJECTED
    assert outcome.bundle.cycle.disposition is Disposition.REJECTED
    assert outcome.bundle.proposal is not None
    assert outcome.bundle.proposal.status is ProposalStatus.REJECTED
    assert outcome.counterfactual is not None
    assert outcome.counterfactual.proposal_id == outcome.bundle.proposal.id
    assert outcome.counterfactual.rejection_gate is GateType.MONITORING
    assert outcome.capability is None
    assert outcome.execution is None
    assert outcome.incident is None
    assert outcome.bundle.summary.cost_snapshot["round_trip_fee_fraction"] == "0.001"
    monitoring_gate = next(
        gate for gate in outcome.bundle.gates if gate.gate_type is GateType.MONITORING
    )
    assert not monitoring_gate.passed
    assert monitoring_gate.output["raw_reason"] == "kill_switch_active"


async def test_fully_admitted_direction_executes_and_records_sensitivities() -> None:
    entry_context = _entry_context()
    command = _command(admitted=True, entry_context=entry_context)
    service = _execution_service(_FeatureComputer(_features()), Direction.LONG)

    outcome = await service.process(command)

    assert outcome.disposition is OrchestrationDisposition.EXECUTED
    assert outcome.bundle.cycle.disposition is Disposition.APPROVED
    assert outcome.bundle.proposal is not None
    assert outcome.bundle.proposal.status is ProposalStatus.APPROVED
    assert all(gate.passed for gate in outcome.bundle.gates)
    assert outcome.incident is None
    assert outcome.counterfactual is None
    assert outcome.capability is not None
    assert outcome.execution is not None
    assert outcome.execution.account is not None
    assert outcome.execution.account.reconcile().ok
    assert len(outcome.sensitivities) == 3
    assert (
        outcome.capability.claims.gate_chain_hash
        == outcome.bundle.proposal.entry_policy["gate_chain_hash"]
    )


async def test_actual_agents_execute_a_small_atr_signal_with_fee_safe_capped_sizing() -> None:
    features = replace(
        _features(),
        regime=Regime.TRENDING,
        zscore=2.0,
        zscore_mean=100.0,
        zscore_std=1.0,
        ema_fast=102.0,
        ema_slow=100.0,
        ema_signal=2.0,
        roc_short=0.05,
        roc_long=0.1,
        atr=0.1,
        rolling_std=0.003,
        bid_ask_spread=0.0001,
        book_imbalance=0.9,
        funding_rate=-0.001,
        annualized_funding=-1.095,
        funding_bias="short_heavy",
    )
    command = _command(admitted=True, entry_context=_entry_context())
    signing_key = b"actual agent capped-sizing test key"
    authorizer = ExecutionAuthorizer(signing_key)
    broker = PaperBroker(
        clock=DeterministicClock(lambda: command.completed_at),
        authorizer=authorizer,
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )
    service = OfficialOrchestrationService(
        _FeatureComputer(features),
        authorizer=authorizer,
        paper_broker=broker,
    )

    outcome = await service.process(command)

    assert outcome.disposition is OrchestrationDisposition.EXECUTED
    assert outcome.bundle.cycle.direction is Direction.LONG
    assert outcome.bundle.proposal is not None
    assert outcome.bundle.proposal.status is ProposalStatus.APPROVED
    risk_input = outcome.bundle.proposal.sizing_snapshot["risk_input"]
    risk_gates = outcome.bundle.proposal.sizing_snapshot["risk_gates"]
    assert isinstance(risk_input, Mapping)
    assert isinstance(risk_gates, tuple)
    risk_request = risk_input["request"]
    assert isinstance(risk_request, Mapping)
    assert risk_request["entry_fee_fraction"] == "0.0005"
    trade_risk_gate = next(
        gate for gate in risk_gates if gate["check"] == RiskCheck.TRADE_RISK_AT_STOP
    )
    assert "margin_and_entry_fee" in trade_risk_gate["details"]["limiting_constraints"]
    margin_gate = next(gate for gate in risk_gates if gate["check"] == RiskCheck.MARGIN)
    assert margin_gate["reason_code"] == "margin_and_entry_fee_within_limit"
    assert Decimal(margin_gate["details"]["entry_fee"]) > 0
    assert Decimal(margin_gate["details"]["total_required_capital"]) <= Decimal(
        margin_gate["details"]["limit"]
    )
    assert all(gate.passed for gate in outcome.bundle.gates)
    assert outcome.execution is not None
    assert len(outcome.execution.fills) == 1
    assert outcome.execution.account is not None
    assert outcome.execution.account.reconcile().ok


async def test_approved_but_unfillable_entry_becomes_operator_review_halt() -> None:
    entry_context = _entry_context()
    book = entry_context.books[0]
    shallow = replace(
        book,
        bids=(BookLevel(book.best_bid, Decimal("0.1")),),
        asks=(BookLevel(book.best_ask, Decimal("0.1")),),
    )
    command = _command(
        admitted=True,
        entry_context=replace(entry_context, books=(shallow,)),
    )
    service = _execution_service(_FeatureComputer(_features()), Direction.LONG)

    outcome = await service.process(command)

    assert outcome.disposition is OrchestrationDisposition.HALTED
    assert outcome.bundle.cycle.disposition is Disposition.APPROVED
    assert outcome.capability is not None
    assert outcome.execution is None
    assert outcome.incident is not None
    assert outcome.incident.component == "paper_execution"
    assert outcome.incident.reason_code == "approved_entry_execution_failed"
    assert outcome.incident.requires_operator_review
