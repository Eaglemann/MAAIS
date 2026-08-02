from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from maais.config.constants import ALL_AGENTS
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    ProposalStatus,
    QualityStatus,
    ReasonCode,
)
from maais.domain.json import JsonValue, content_hash, freeze_json, to_json_data


def _validate_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC-aware")


def _validate_uuid(name: str, value: UUID) -> None:
    if value.int == 0:
        raise ValueError(f"{name} cannot be nil")


def _validate_decimal(name: str, value: Decimal, *, nonnegative: bool = False) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if nonnegative and value < 0:
        raise ValueError(f"{name} cannot be negative")


def _validate_unit_interval(name: str, value: Decimal) -> None:
    _validate_decimal(name, value)
    if value < 0 or value > 1:
        raise ValueError(f"{name} must be in [0, 1]")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("expected a JSON object")
    return frozen


@dataclass(frozen=True, slots=True)
class MarketFrameRecord:
    id: UUID
    experiment_id: UUID
    symbol: str
    venue: str
    timeframe: str
    bar_open_at: datetime
    bar_close_at: datetime
    observed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    mark_price: Decimal | None
    funding_rate: Decimal | None
    orderbook_snapshot: Mapping[str, JsonValue]
    source_sequence: Mapping[str, JsonValue]
    quality_status: QualityStatus
    quality_results: Mapping[str, JsonValue]
    content_hash: str

    def __post_init__(self) -> None:
        _validate_uuid("market_frame.id", self.id)
        _validate_uuid("market_frame.experiment_id", self.experiment_id)
        for name in ("bar_open_at", "bar_close_at", "observed_at"):
            _validate_utc(name, getattr(self, name))
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("market frame symbol must be uppercase")
        if not self.venue or not self.timeframe:
            raise ValueError("venue and timeframe are required")
        if not self.bar_open_at < self.bar_close_at <= self.observed_at:
            raise ValueError("market frame times are not ordered")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            _validate_decimal(name, value)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        _validate_decimal("volume", self.volume, nonnegative=True)
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        for name in ("best_bid", "best_ask", "mark_price", "funding_rate"):
            value = getattr(self, name)
            if value is not None:
                _validate_decimal(name, value)
        if len(self.content_hash) != 64:
            raise ValueError("market frame content_hash must be SHA-256")
        for name in ("orderbook_snapshot", "source_sequence", "quality_results"):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class DecisionCycleRecord:
    id: UUID
    experiment_id: UUID
    market_frame_id: UUID
    strategy_version_id: UUID
    symbol: str
    timeframe: str
    cycle_at: datetime
    regime: str
    feature_snapshot: Mapping[str, JsonValue]
    feature_version: str
    status: DecisionStatus
    direction: Direction
    disposition: Disposition
    reason_code: ReasonCode
    created_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "experiment_id", "market_frame_id", "strategy_version_id"):
            _validate_uuid(name, getattr(self, name))
        for name in ("cycle_at", "created_at", "completed_at"):
            _validate_utc(name, getattr(self, name))
        if not self.created_at <= self.completed_at:
            raise ValueError("decision completion cannot precede creation")
        if not self.symbol or not self.timeframe or not self.regime or not self.feature_version:
            raise ValueError("decision identity and version fields are required")
        object.__setattr__(self, "feature_snapshot", _freeze_mapping(self.feature_snapshot))


@dataclass(frozen=True, slots=True)
class AgentEvaluationRecord:
    id: UUID
    decision_cycle_id: UUID
    agent_version_id: UUID
    agent_name: str
    compatible: bool
    enabled: bool
    weight: Decimal
    direction: Direction
    probability: Decimal
    confidence: Decimal
    risk: Decimal
    input_snapshot: Mapping[str, JsonValue]
    reason_codes: tuple[ReasonCode, ...]
    explanation: Mapping[str, JsonValue]
    duration_ms: int
    created_at: datetime

    def __post_init__(self) -> None:
        for name in ("id", "decision_cycle_id", "agent_version_id"):
            _validate_uuid(name, getattr(self, name))
        _validate_utc("agent created_at", self.created_at)
        _validate_decimal("weight", self.weight)
        if self.weight <= 0:
            raise ValueError("agent weight must be positive")
        for name in ("probability", "confidence", "risk"):
            _validate_unit_interval(name, getattr(self, name))
        if self.duration_ms < 0:
            raise ValueError("agent duration cannot be negative")
        object.__setattr__(self, "input_snapshot", _freeze_mapping(self.input_snapshot))
        object.__setattr__(self, "explanation", _freeze_mapping(self.explanation))


@dataclass(frozen=True, slots=True)
class DecisionSummaryRecord:
    decision_cycle_id: UUID
    consensus_direction: Direction
    consensus_probability: Decimal
    consensus_confidence: Decimal
    long_weight: Decimal
    short_weight: Decimal
    neutral_weight: Decimal
    dissenters: tuple[str, ...]
    dissent_probability: Decimal
    dissent_confidence: Decimal
    challenge_blocked: bool
    expected_gain: Decimal
    expected_loss: Decimal
    gross_ev: Decimal
    funding_carry: Decimal
    estimated_cost: Decimal
    net_ev: Decimal
    benchmark_return: Decimal
    alpha_estimate: Decimal
    consensus_snapshot: Mapping[str, JsonValue]
    adversarial_snapshot: Mapping[str, JsonValue]
    ev_snapshot: Mapping[str, JsonValue]
    cost_snapshot: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_uuid("decision_cycle_id", self.decision_cycle_id)
        for name in (
            "consensus_probability",
            "consensus_confidence",
            "dissent_probability",
            "dissent_confidence",
        ):
            _validate_unit_interval(name, getattr(self, name))
        for name in (
            "long_weight",
            "short_weight",
            "neutral_weight",
            "expected_gain",
            "expected_loss",
            "estimated_cost",
        ):
            _validate_decimal(name, getattr(self, name), nonnegative=True)
        for name in ("gross_ev", "funding_carry", "net_ev", "benchmark_return", "alpha_estimate"):
            _validate_decimal(name, getattr(self, name))
        for name in (
            "consensus_snapshot",
            "adversarial_snapshot",
            "ev_snapshot",
            "cost_snapshot",
        ):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class GateEvaluationRecord:
    id: UUID
    decision_cycle_id: UUID
    gate_type: GateType
    sequence: int
    passed: bool
    reason_code: ReasonCode
    input: Mapping[str, JsonValue]
    output: Mapping[str, JsonValue]
    evaluated_at: datetime
    duration_ms: int

    def __post_init__(self) -> None:
        _validate_uuid("gate.id", self.id)
        _validate_uuid("gate.decision_cycle_id", self.decision_cycle_id)
        _validate_utc("gate.evaluated_at", self.evaluated_at)
        if self.sequence < 1:
            raise ValueError("gate sequence must start at one")
        if self.duration_ms < 0:
            raise ValueError("gate duration cannot be negative")
        object.__setattr__(self, "input", _freeze_mapping(self.input))
        object.__setattr__(self, "output", _freeze_mapping(self.output))


@dataclass(frozen=True, slots=True)
class TradeProposalRecord:
    id: UUID
    decision_cycle_id: UUID
    experiment_id: UUID
    symbol: str
    direction: Direction
    status: ProposalStatus
    reason_code: ReasonCode
    proposed_at: datetime
    expires_at: datetime
    entry_policy: Mapping[str, JsonValue]
    exit_policy: Mapping[str, JsonValue]
    sizing_snapshot: Mapping[str, JsonValue]
    approved_quantity: Decimal | None
    approved_notional: Decimal | None
    risk_at_stop: Decimal | None

    def __post_init__(self) -> None:
        for name in ("id", "decision_cycle_id", "experiment_id"):
            _validate_uuid(name, getattr(self, name))
        _validate_utc("proposed_at", self.proposed_at)
        _validate_utc("expires_at", self.expires_at)
        if self.expires_at <= self.proposed_at:
            raise ValueError("proposal expiry must follow proposal time")
        for name in ("approved_quantity", "approved_notional", "risk_at_stop"):
            value = getattr(self, name)
            if value is not None:
                _validate_decimal(name, value, nonnegative=True)
        for name in ("entry_policy", "exit_policy", "sizing_snapshot"):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name)))


def _record_tree(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _record_tree(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_record_tree(item) for item in value]
    return value


def record_to_dict(value: object) -> dict[str, object]:
    normalized = to_json_data(_record_tree(value))
    if not isinstance(normalized, dict):
        raise TypeError("record must normalize to an object")
    return cast(dict[str, object], normalized)


@dataclass(frozen=True, slots=True)
class DecisionBundle:
    market_frame: MarketFrameRecord
    cycle: DecisionCycleRecord
    agents: tuple[AgentEvaluationRecord, ...]
    summary: DecisionSummaryRecord
    gates: tuple[GateEvaluationRecord, ...]
    proposal: TradeProposalRecord | None

    def validate(self) -> None:
        if self.market_frame.experiment_id != self.cycle.experiment_id:
            raise ValueError("market frame and cycle experiment differ")
        if self.market_frame.id != self.cycle.market_frame_id:
            raise ValueError("cycle does not reference its market frame")
        if (self.market_frame.symbol, self.market_frame.timeframe) != (
            self.cycle.symbol,
            self.cycle.timeframe,
        ):
            raise ValueError("market frame and cycle identity differ")
        agents_by_name = {agent.agent_name: agent for agent in self.agents}
        if len(self.agents) != len(ALL_AGENTS) or set(agents_by_name) != set(ALL_AGENTS):
            raise ValueError("bundle requires exactly one evaluation for each configured agent")
        for name in ALL_AGENTS:
            agent = agents_by_name[name]
            if agent.decision_cycle_id != self.cycle.id:
                raise ValueError("agent references another decision cycle")
            if not agent.enabled and ReasonCode.DISABLED_AGENT not in agent.reason_codes:
                raise ValueError("disabled_agent reason is required")
            if not agent.compatible and not {
                ReasonCode.INCOMPATIBLE_REGIME,
                ReasonCode.AGENT_FAILED,
            }.intersection(agent.reason_codes):
                raise ValueError("incompatible or failed-agent reason is required")
            if (
                not agent.enabled or not agent.compatible
            ) and agent.direction is not Direction.NEUTRAL:
                raise ValueError("non-voting agents must be neutral")
        if self.summary.decision_cycle_id != self.cycle.id:
            raise ValueError("summary references another decision cycle")
        expected_sequences = list(range(1, len(self.gates) + 1))
        if [gate.sequence for gate in self.gates] != expected_sequences:
            raise ValueError("gate sequences must be contiguous from one")
        if len({gate.gate_type for gate in self.gates}) != len(self.gates):
            raise ValueError("gate types cannot repeat")
        failure_seen = False
        for gate in self.gates:
            if gate.decision_cycle_id != self.cycle.id:
                raise ValueError("gate references another decision cycle")
            if failure_seen and gate.passed:
                raise ValueError("a gate passed after failure")
            failure_seen = failure_seen or not gate.passed

        if self.cycle.disposition is Disposition.NEUTRAL:
            if self.cycle.direction is not Direction.NEUTRAL or self.proposal is not None:
                raise ValueError("neutral cycle cannot create a synthetic proposal")
        else:
            if self.cycle.direction is Direction.NEUTRAL or self.proposal is None:
                raise ValueError("directional cycle requires a proposal")
            if (
                self.proposal.decision_cycle_id != self.cycle.id
                or self.proposal.experiment_id != self.cycle.experiment_id
                or self.proposal.symbol != self.cycle.symbol
                or self.proposal.direction is not self.cycle.direction
            ):
                raise ValueError("proposal identity does not match decision cycle")
            expected_status = (
                ProposalStatus.APPROVED
                if self.cycle.disposition is Disposition.APPROVED
                else ProposalStatus.REJECTED
            )
            if self.proposal.status is not expected_status:
                raise ValueError("proposal status does not match disposition")
        if self.cycle.disposition is Disposition.APPROVED and failure_seen:
            raise ValueError("approved decision cannot contain a failed gate")
        if self.cycle.disposition is Disposition.REJECTED and not failure_seen:
            raise ValueError("rejected decision requires a failed gate")

    def to_dict(self) -> dict[str, object]:
        return record_to_dict(self)

    @property
    def bundle_hash(self) -> str:
        self.validate()
        return content_hash(self.to_dict())
