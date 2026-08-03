from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, cast
from uuid import UUID

from maais.agents.base import AgentOutput, BaseAgent
from maais.agents.runner import build_agent_registry
from maais.config.constants import ALL_AGENTS, AgentName
from maais.config.modes import RunMode
from maais.domain.enums import AgentMaturity, Direction
from maais.domain.json import JsonValue, content_hash, freeze_json, to_json_data
from maais.experiments.manifest import AgentManifestEntry, ExperimentManifest
from maais.feature_pipeline.features import FeatureSet


class DurationSource(Protocol):
    def start(self, agent_name: str) -> int: ...

    def elapsed_ms(self, agent_name: str, started: int) -> int: ...


class MonotonicDurationSource:
    def start(self, agent_name: str) -> int:
        return time.monotonic_ns()

    def elapsed_ms(self, agent_name: str, started: int) -> int:
        return max(0, (time.monotonic_ns() - started) // 1_000_000)


class DeterministicDurationSource:
    def __init__(self, durations: Mapping[str, int] | None = None) -> None:
        values = dict(durations or {})
        if any(name not in ALL_AGENTS or value < 0 for name, value in values.items()):
            raise ValueError("deterministic agent durations must be named and nonnegative")
        self._durations = values

    def start(self, agent_name: str) -> int:
        return 0

    def elapsed_ms(self, agent_name: str, started: int) -> int:
        return self._durations.get(agent_name, 0)


@dataclass(frozen=True, slots=True)
class AgentOutputSnapshot:
    direction: Direction
    probability: Decimal
    confidence: Decimal
    risk: Decimal

    def __post_init__(self) -> None:
        for field_name in ("probability", "confidence", "risk"):
            value = getattr(self, field_name)
            if not value.is_finite() or value < 0 or value > 1:
                raise ValueError(f"agent {field_name} must be a finite Decimal in [0, 1]")

    @classmethod
    def neutral(cls, *, risk: Decimal = Decimal("0.5")) -> AgentOutputSnapshot:
        return cls(Direction.NEUTRAL, Decimal("0.5"), Decimal("0"), risk)

    @classmethod
    def from_legacy(cls, output: AgentOutput) -> AgentOutputSnapshot:
        return cls(
            direction=Direction(output.directional_hypothesis),
            probability=Decimal(str(output.probability_estimate)),
            confidence=Decimal(str(output.confidence_score)),
            risk=Decimal(str(output.risk_estimate)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "probability": self.probability,
            "confidence": self.confidence,
            "risk": self.risk,
        }


@dataclass(frozen=True, slots=True)
class AgentEvaluation:
    agent_name: str
    version: str
    maturity: AgentMaturity
    proxy_label: str | None
    weight: Decimal
    enabled: bool
    compatible: bool
    voting: bool
    reason_codes: tuple[str, ...]
    input_contributions: Mapping[str, JsonValue]
    duration_ms: int
    output: AgentOutputSnapshot
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.agent_name not in ALL_AGENTS or not self.version:
            raise ValueError("agent evaluation identity is invalid")
        if not self.weight.is_finite() or self.weight <= 0:
            raise ValueError("agent evaluation weight must be positive and finite")
        if self.duration_ms < 0 or not self.reason_codes:
            raise ValueError("agent evaluation duration and reason codes are invalid")
        if self.maturity is AgentMaturity.PROXY and not self.proxy_label:
            raise ValueError("proxy agent evaluation requires a visible proxy label")
        if self.maturity is not AgentMaturity.PROXY and self.proxy_label is not None:
            raise ValueError("only a proxy agent can have a proxy label")
        if self.voting and (not self.enabled or not self.compatible or self.failure_reason):
            raise ValueError("only a successful enabled compatible agent can vote")
        if not self.voting and self.output.direction is not Direction.NEUTRAL:
            raise ValueError("nonvoting agent output must be neutral")
        normalized = freeze_json(self.input_contributions)
        if not isinstance(normalized, Mapping):
            raise TypeError("agent input contributions must be an object")
        object.__setattr__(self, "input_contributions", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "version": self.version,
            "maturity": self.maturity,
            "proxy_label": self.proxy_label,
            "weight": self.weight,
            "enabled": self.enabled,
            "compatible": self.compatible,
            "voting": self.voting,
            "reason_codes": self.reason_codes,
            "input_contributions": self.input_contributions,
            "duration_ms": self.duration_ms,
            "output": self.output.to_dict(),
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class AgentEvaluationMatrix:
    experiment_id: UUID
    symbol: str
    timeframe: str
    feature_at: datetime
    regime: str | None
    evaluations: tuple[AgentEvaluation, ...]

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0:
            raise ValueError("agent matrix experiment_id cannot be nil")
        if not self.symbol or self.symbol != self.symbol.upper() or not self.timeframe:
            raise ValueError("agent matrix market identity is invalid")
        if self.feature_at.tzinfo is None or self.feature_at.utcoffset() != timedelta(0):
            raise ValueError("agent matrix feature_at must be UTC-aware")
        names = tuple(item.agent_name for item in self.evaluations)
        if names != ALL_AGENTS:
            raise ValueError("agent evaluation matrix must contain exactly eight ordered rows")

    def by_name(self, agent_name: str) -> AgentEvaluation:
        return next(item for item in self.evaluations if item.agent_name == agent_name)

    @property
    def content_hash(self) -> str:
        return content_hash(
            {
                "experiment_id": self.experiment_id,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "feature_at": self.feature_at,
                "regime": self.regime,
                "evaluations": [item.to_dict() for item in self.evaluations],
            }
        )

    def to_dict(self) -> dict[str, object]:
        normalized = to_json_data(
            {
                "experiment_id": self.experiment_id,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "feature_at": self.feature_at,
                "regime": self.regime,
                "evaluations": [item.to_dict() for item in self.evaluations],
                "content_hash": self.content_hash,
            }
        )
        if not isinstance(normalized, dict):
            raise TypeError("agent matrix must normalize to an object")
        return cast(dict[str, object], normalized)


@dataclass(frozen=True, slots=True)
class AgentFailure:
    agent_name: str
    reason_code: str
    details: str


class AgentMatrixError(RuntimeError):
    def __init__(
        self,
        message: str,
        matrix: AgentEvaluationMatrix,
        failures: tuple[AgentFailure, ...],
    ) -> None:
        super().__init__(message)
        self.matrix = matrix
        self.failures = failures


def _proxy_label(entry: AgentManifestEntry) -> str | None:
    if entry.maturity is not AgentMaturity.PROXY:
        return None
    if entry.agent_name == AgentName.MACRO_SENTIMENT:
        return "technical_features_proxy"
    return "proxy"


def _inputs(entry: AgentManifestEntry, features: FeatureSet) -> Mapping[str, JsonValue]:
    normalized = freeze_json(
        {
            "declared_dependencies": entry.data_dependencies,
            "feature_snapshot": features.to_dict(),
        }
    )
    if not isinstance(normalized, Mapping):
        raise TypeError("agent inputs must be an object")
    return normalized


def _neutral_evaluation(
    entry: AgentManifestEntry,
    features: FeatureSet,
    *,
    compatible: bool,
    reason_code: str,
    failure_reason: str | None = None,
    duration_ms: int = 0,
) -> AgentEvaluation:
    return AgentEvaluation(
        agent_name=entry.agent_name,
        version=entry.version,
        maturity=entry.maturity,
        proxy_label=_proxy_label(entry),
        weight=entry.weight,
        enabled=entry.enabled,
        compatible=compatible,
        voting=False,
        reason_codes=(reason_code,),
        input_contributions=_inputs(entry, features),
        duration_ms=duration_ms,
        output=AgentOutputSnapshot.neutral(risk=Decimal("1") if failure_reason else Decimal("0.5")),
        failure_reason=failure_reason,
    )


def _registry_failure_matrix(
    manifest: ExperimentManifest,
    features: FeatureSet,
    agents: Sequence[BaseAgent],
) -> tuple[AgentEvaluationMatrix, tuple[AgentFailure, ...]]:
    counts = Counter(agent.name for agent in agents)
    failures: list[AgentFailure] = []
    rows: list[AgentEvaluation] = []
    for entry in manifest.agent_versions:
        count = counts.get(entry.agent_name, 0)
        if count == 0:
            reason = "agent_missing"
            failures.append(AgentFailure(entry.agent_name, reason, "configured agent is missing"))
        elif count > 1:
            reason = "agent_duplicate"
            failures.append(
                AgentFailure(entry.agent_name, reason, "configured agent appears more than once")
            )
        else:
            reason = "agent_registry_invalid"
        rows.append(
            _neutral_evaluation(
                entry,
                features,
                compatible=False,
                reason_code=reason,
                failure_reason=reason,
            )
        )
    unknown = sorted(name for name in counts if name not in ALL_AGENTS)
    failures.extend(
        AgentFailure(name, "agent_unknown", "unknown registry agent") for name in unknown
    )
    return (
        AgentEvaluationMatrix(
            manifest.experiment_id,
            features.symbol,
            features.timeframe,
            features.timestamp,
            features.regime,
            tuple(rows),
        ),
        tuple(failures),
    )


async def run_agent_matrix(
    features: FeatureSet,
    manifest: ExperimentManifest,
    *,
    agents: Sequence[BaseAgent] | None = None,
    durations: DurationSource | None = None,
) -> AgentEvaluationMatrix:
    registry = tuple(agents if agents is not None else build_agent_registry())
    counts = Counter(agent.name for agent in registry)
    if set(counts) != set(ALL_AGENTS) or any(count != 1 for count in counts.values()):
        matrix, failures = _registry_failure_matrix(manifest, features, registry)
        raise AgentMatrixError("agent registry does not match the manifest", matrix, failures)
    agents_by_name = {agent.name: agent for agent in registry}
    duration_source = durations or (
        DeterministicDurationSource()
        if manifest.mode is RunMode.REPLAY
        else MonotonicDurationSource()
    )

    async def evaluate(entry: AgentManifestEntry) -> tuple[AgentEvaluation, AgentFailure | None]:
        agent = agents_by_name[entry.agent_name]
        compatible = agent.is_compatible_with_regime(features.regime)
        if not entry.enabled:
            return (
                _neutral_evaluation(
                    entry,
                    features,
                    compatible=compatible,
                    reason_code="disabled_agent",
                ),
                None,
            )
        if not compatible:
            return (
                _neutral_evaluation(
                    entry,
                    features,
                    compatible=False,
                    reason_code="incompatible_regime",
                ),
                None,
            )
        started = duration_source.start(entry.agent_name)
        try:
            output = await agent.analyze(features)
            if output.agent_name != entry.agent_name:
                raise ValueError(
                    f"agent output identity {output.agent_name!r} differs from {entry.agent_name!r}"
                )
            snapshot = AgentOutputSnapshot.from_legacy(output)
            duration_ms = duration_source.elapsed_ms(entry.agent_name, started)
            return (
                AgentEvaluation(
                    agent_name=entry.agent_name,
                    version=entry.version,
                    maturity=entry.maturity,
                    proxy_label=_proxy_label(entry),
                    weight=entry.weight,
                    enabled=True,
                    compatible=True,
                    voting=True,
                    reason_codes=("agent_evaluated",),
                    input_contributions=_inputs(entry, features),
                    duration_ms=duration_ms,
                    output=snapshot,
                ),
                None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            details = f"{type(exc).__name__}: {exc}"
            duration_ms = duration_source.elapsed_ms(entry.agent_name, started)
            return (
                _neutral_evaluation(
                    entry,
                    features,
                    compatible=True,
                    reason_code="agent_exception",
                    failure_reason=details,
                    duration_ms=duration_ms,
                ),
                AgentFailure(entry.agent_name, "agent_exception", details),
            )

    results = await asyncio.gather(*(evaluate(entry) for entry in manifest.agent_versions))
    matrix = AgentEvaluationMatrix(
        manifest.experiment_id,
        features.symbol,
        features.timeframe,
        features.timestamp,
        features.regime,
        tuple(result[0] for result in results),
    )
    failures = tuple(result[1] for result in results if result[1] is not None)
    if failures:
        raise AgentMatrixError("one or more mandatory agents failed", matrix, failures)
    return matrix
