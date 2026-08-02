from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from maais.agents.base import BaseAgent
from maais.agents.evaluations import (
    AgentEvaluationMatrix,
    AgentMatrixError,
    DurationSource,
    run_agent_matrix,
)
from maais.decisions.bundle import (
    AgentEvaluationRecord,
    DecisionBundle,
    DecisionCycleRecord,
    DecisionSummaryRecord,
    GateEvaluationRecord,
    MarketFrameRecord,
)
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    ReasonCode,
)
from maais.domain.json import JsonValue, freeze_json
from maais.feature_pipeline.features import FeatureSet
from maais.market_data.frames import CausalMinuteFrame
from maais.market_data.integrity.state_machine import FrameAdmission
from maais.operations.incidents import IncidentSeverity, IncidentState
from maais.orchestration.commands import OrchestrationCommand
from maais.orchestration.results import OrchestrationDisposition, OrchestrationOutcome


class FeatureComputer(Protocol):
    def compute(self, frame: CausalMinuteFrame) -> FeatureSet | None: ...


def _id(kind: str, *parts: object) -> UUID:
    identity = "/".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, f"maais://{kind}/{identity}")


def _json_object(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("orchestration snapshot must be a JSON object")
    return normalized


class OfficialOrchestrationService:
    """Pure fail-closed assembly of one authoritative decision outcome."""

    def __init__(
        self,
        feature_computer: FeatureComputer,
        *,
        agents: Sequence[BaseAgent] | None = None,
        durations: DurationSource | None = None,
    ) -> None:
        self._feature_computer = feature_computer
        self._agents = tuple(agents) if agents is not None else None
        self._durations = durations

    async def process(self, command: OrchestrationCommand) -> OrchestrationOutcome:
        if command.integrity.admission is FrameAdmission.QUARANTINED:
            return self._quarantined(command)

        features = self._feature_computer.compute(command.frame)
        if features is None:
            return self._feature_failure(command, "feature_history_insufficient")
        self._validate_features(command, features)
        try:
            matrix = await run_agent_matrix(
                features,
                command.manifest,
                agents=self._agents,
                durations=self._durations,
            )
        except AgentMatrixError as exc:
            return self._agent_failure(command, features, exc.matrix, exc)
        return self._admitted_placeholder(command, features, matrix)

    def _quarantined(self, command: OrchestrationCommand) -> OrchestrationOutcome:
        cycle_id = self._cycle_id(command)
        agents = tuple(
            AgentEvaluationRecord(
                id=_id("agent-evaluation", cycle_id, entry.agent_name),
                decision_cycle_id=cycle_id,
                agent_version_id=command.agent_version_ids[entry.agent_name],
                agent_name=entry.agent_name,
                compatible=True,
                enabled=entry.enabled,
                weight=entry.weight,
                direction=Direction.NEUTRAL,
                probability=Decimal("0.5"),
                confidence=Decimal("0"),
                risk=Decimal("1"),
                input_snapshot={
                    "frame_content_hash": command.frame.content_hash,
                    "integrity_hash": command.integrity.content_hash,
                    "feature_execution": "skipped",
                },
                reason_codes=(
                    (ReasonCode.DATA_QUALITY_FAILED, ReasonCode.DISABLED_AGENT)
                    if not entry.enabled
                    else (ReasonCode.DATA_QUALITY_FAILED,)
                ),
                explanation={
                    "admission": command.integrity.admission,
                    "blocking_checks": command.integrity.blocking_checks,
                    "maturity": entry.maturity,
                    "voting": False,
                },
                duration_ms=0,
                created_at=command.evaluated_at,
            )
            for entry in command.manifest.agent_versions
        )
        bundle = DecisionBundle(
            market_frame=self._market_frame(command),
            cycle=self._cycle(
                command,
                cycle_id=cycle_id,
                status=DecisionStatus.QUARANTINED,
                reason=ReasonCode.DATA_QUALITY_FAILED,
                regime="quarantined",
                feature_snapshot={"execution": "skipped_due_to_data_quality"},
            ),
            agents=agents,
            summary=self._neutral_summary(cycle_id, "data_quality_quarantine"),
            gates=(
                self._gate(
                    cycle_id,
                    GateType.DATA_QUALITY,
                    1,
                    False,
                    ReasonCode.DATA_QUALITY_FAILED,
                    input={"integrity_hash": command.integrity.content_hash},
                    output={
                        "admission": command.integrity.admission,
                        "blocking_checks": command.integrity.blocking_checks,
                    },
                    command=command,
                ),
            ),
            proposal=None,
        )
        incident = self._incident(
            command,
            component="market_data",
            reason="market_frame_quarantined",
            evidence={
                "frame_id": command.frame.frame_id,
                "frame_content_hash": command.frame.content_hash,
                "integrity_hash": command.integrity.content_hash,
                "blocking_checks": command.integrity.blocking_checks,
                "results": [item.to_dict() for item in command.integrity.results],
            },
            severity=IncidentSeverity.ERROR,
        )
        return OrchestrationOutcome(OrchestrationDisposition.QUARANTINED, bundle, incident)

    def _feature_failure(
        self,
        command: OrchestrationCommand,
        reason: str,
    ) -> OrchestrationOutcome:
        return self._neutral_halt(
            command,
            reason_code=ReasonCode.INSUFFICIENT_HISTORY,
            component="features",
            incident_reason=reason,
            feature_snapshot={"failure_reason": reason},
        )

    def _agent_failure(
        self,
        command: OrchestrationCommand,
        features: FeatureSet,
        matrix: AgentEvaluationMatrix,
        error: AgentMatrixError,
    ) -> OrchestrationOutcome:
        cycle_id = self._cycle_id(command)
        records = self._matrix_records(command, cycle_id, matrix)
        bundle = DecisionBundle(
            market_frame=self._market_frame(command),
            cycle=self._cycle(
                command,
                cycle_id=cycle_id,
                status=DecisionStatus.REJECTED,
                reason=ReasonCode.AGENT_FAILED,
                regime=features.regime or "unknown",
                feature_snapshot=features.to_dict(),
            ),
            agents=records,
            summary=self._neutral_summary(cycle_id, "mandatory_agent_failure"),
            gates=(
                self._gate(
                    cycle_id,
                    GateType.DATA_QUALITY,
                    1,
                    True,
                    ReasonCode.ACCEPTED,
                    input={"integrity_hash": command.integrity.content_hash},
                    output={"admission": command.integrity.admission},
                    command=command,
                ),
                self._gate(
                    cycle_id,
                    GateType.CONSENSUS,
                    2,
                    False,
                    ReasonCode.AGENT_FAILED,
                    input={"matrix_hash": matrix.content_hash},
                    output={
                        "failures": [
                            {
                                "agent_name": failure.agent_name,
                                "reason_code": failure.reason_code,
                                "details": failure.details,
                            }
                            for failure in error.failures
                        ]
                    },
                    command=command,
                ),
            ),
            proposal=None,
        )
        incident = self._incident(
            command,
            component="agents",
            reason="mandatory_agent_failure",
            evidence={
                "decision_cycle_id": cycle_id,
                "matrix_hash": matrix.content_hash,
                "failures": [
                    {
                        "agent_name": failure.agent_name,
                        "reason_code": failure.reason_code,
                        "details": failure.details,
                    }
                    for failure in error.failures
                ],
            },
            severity=IncidentSeverity.CRITICAL,
        )
        return OrchestrationOutcome(OrchestrationDisposition.HALTED, bundle, incident)

    def _admitted_placeholder(
        self,
        command: OrchestrationCommand,
        features: FeatureSet,
        matrix: AgentEvaluationMatrix,
    ) -> OrchestrationOutcome:
        # The admitted consensus/risk path is completed in the next isolated
        # tranche. Until then it is explicitly fail-closed and cannot propose.
        cycle_id = self._cycle_id(command)
        bundle = DecisionBundle(
            market_frame=self._market_frame(command),
            cycle=self._cycle(
                command,
                cycle_id=cycle_id,
                status=DecisionStatus.REJECTED,
                reason=ReasonCode.CONSENSUS_FAILED,
                regime=features.regime or "unknown",
                feature_snapshot=features.to_dict(),
            ),
            agents=self._matrix_records(command, cycle_id, matrix),
            summary=self._neutral_summary(cycle_id, "official_consensus_not_completed"),
            gates=(
                self._gate(
                    cycle_id,
                    GateType.DATA_QUALITY,
                    1,
                    True,
                    ReasonCode.ACCEPTED,
                    input={"integrity_hash": command.integrity.content_hash},
                    output={"admission": command.integrity.admission},
                    command=command,
                ),
                self._gate(
                    cycle_id,
                    GateType.CONSENSUS,
                    2,
                    False,
                    ReasonCode.CONSENSUS_FAILED,
                    input={"matrix_hash": matrix.content_hash},
                    output={"reason": "official_consensus_not_completed"},
                    command=command,
                ),
            ),
            proposal=None,
        )
        incident = self._incident(
            command,
            component="orchestration",
            reason="official_consensus_not_completed",
            evidence={"matrix_hash": matrix.content_hash},
            severity=IncidentSeverity.CRITICAL,
        )
        return OrchestrationOutcome(OrchestrationDisposition.HALTED, bundle, incident)

    def _neutral_halt(
        self,
        command: OrchestrationCommand,
        *,
        reason_code: ReasonCode,
        component: str,
        incident_reason: str,
        feature_snapshot: Mapping[str, object],
    ) -> OrchestrationOutcome:
        cycle_id = self._cycle_id(command)
        normalized_feature_snapshot = _json_object(feature_snapshot)
        agents = tuple(
            AgentEvaluationRecord(
                id=_id("agent-evaluation", cycle_id, entry.agent_name),
                decision_cycle_id=cycle_id,
                agent_version_id=command.agent_version_ids[entry.agent_name],
                agent_name=entry.agent_name,
                compatible=True,
                enabled=entry.enabled,
                weight=entry.weight,
                direction=Direction.NEUTRAL,
                probability=Decimal("0.5"),
                confidence=Decimal("0"),
                risk=Decimal("1"),
                input_snapshot=normalized_feature_snapshot,
                reason_codes=(
                    (reason_code, ReasonCode.DISABLED_AGENT)
                    if not entry.enabled
                    else (reason_code,)
                ),
                explanation={"voting": False, "failure_reason": incident_reason},
                duration_ms=0,
                created_at=command.evaluated_at,
            )
            for entry in command.manifest.agent_versions
        )
        bundle = DecisionBundle(
            self._market_frame(command),
            self._cycle(
                command,
                cycle_id=cycle_id,
                status=DecisionStatus.REJECTED,
                reason=reason_code,
                regime="unavailable",
                feature_snapshot=normalized_feature_snapshot,
            ),
            agents,
            self._neutral_summary(cycle_id, incident_reason),
            (
                self._gate(
                    cycle_id,
                    GateType.DATA_QUALITY,
                    1,
                    True,
                    ReasonCode.ACCEPTED,
                    input={"integrity_hash": command.integrity.content_hash},
                    output={"admission": command.integrity.admission},
                    command=command,
                ),
                self._gate(
                    cycle_id,
                    GateType.CONSENSUS,
                    2,
                    False,
                    reason_code,
                    input=normalized_feature_snapshot,
                    output={"reason": incident_reason},
                    command=command,
                ),
            ),
            None,
        )
        incident = self._incident(
            command,
            component=component,
            reason=incident_reason,
            evidence={"decision_cycle_id": cycle_id, **normalized_feature_snapshot},
            severity=IncidentSeverity.CRITICAL,
        )
        return OrchestrationOutcome(OrchestrationDisposition.HALTED, bundle, incident)

    @staticmethod
    def _validate_features(command: OrchestrationCommand, features: FeatureSet) -> None:
        if (
            features.symbol != command.frame.key.symbol
            or features.timeframe != command.frame.key.timeframe
            or features.timestamp != command.frame.bar.bar_close_at
        ):
            raise ValueError("feature identity differs from the causal market frame")
        if not features.regime:
            raise ValueError("official feature output requires an explicit regime")

    @staticmethod
    def _cycle_id(command: OrchestrationCommand) -> UUID:
        return _id("decision-cycle", command.frame.frame_id)

    def _market_frame(self, command: OrchestrationCommand) -> MarketFrameRecord:
        frame = command.frame
        book_source = frame.source_manifest.get("order_book")
        closed_bar_source = frame.source_manifest.get("closed_bar")
        return MarketFrameRecord(
            id=frame.frame_id,
            experiment_id=frame.key.experiment_id,
            symbol=frame.key.symbol,
            venue=closed_bar_source.venue if closed_bar_source is not None else "unknown",
            timeframe=frame.key.timeframe,
            bar_open_at=frame.bar.bar_open_at,
            bar_close_at=frame.bar.bar_close_at,
            observed_at=frame.cutoff_at,
            open=frame.bar.open,
            high=frame.bar.high,
            low=frame.bar.low,
            close=frame.bar.close,
            volume=frame.bar.volume,
            best_bid=frame.best_bid,
            best_ask=frame.best_ask,
            mark_price=frame.mark_price,
            funding_rate=frame.funding_rate,
            orderbook_snapshot=_json_object(
                {
                    "best_bid": frame.best_bid,
                    "best_ask": frame.best_ask,
                    "source_event_id": (book_source.event_id if book_source is not None else None),
                    "source_content_hash": (
                        book_source.content_hash if book_source is not None else None
                    ),
                }
            ),
            source_sequence=_json_object(
                {
                    name: {
                        "event_id": source.event_id,
                        "sequence": source.sequence,
                        "content_hash": source.content_hash,
                    }
                    for name, source in sorted(frame.source_manifest.items())
                }
            ),
            quality_status=command.integrity.quality_status,
            quality_results=_json_object(
                {
                    "assessment_hash": command.integrity.content_hash,
                    "admission": command.integrity.admission,
                    "blocking_checks": command.integrity.blocking_checks,
                    "results": [item.to_dict() for item in command.integrity.results],
                }
            ),
            content_hash=frame.content_hash,
        )

    def _cycle(
        self,
        command: OrchestrationCommand,
        *,
        cycle_id: UUID,
        status: DecisionStatus,
        reason: ReasonCode,
        regime: str,
        feature_snapshot: Mapping[str, object],
    ) -> DecisionCycleRecord:
        return DecisionCycleRecord(
            id=cycle_id,
            experiment_id=command.frame.key.experiment_id,
            market_frame_id=command.frame.frame_id,
            strategy_version_id=command.frame.key.strategy_version_id,
            symbol=command.frame.key.symbol,
            timeframe=command.frame.key.timeframe,
            cycle_at=command.frame.bar.bar_close_at,
            regime=regime,
            feature_snapshot=_json_object(feature_snapshot),
            feature_version=command.feature_version,
            status=status,
            direction=Direction.NEUTRAL,
            disposition=Disposition.NEUTRAL,
            reason_code=reason,
            created_at=command.evaluated_at,
            completed_at=command.completed_at,
        )

    @staticmethod
    def _neutral_summary(cycle_id: UUID, reason: str) -> DecisionSummaryRecord:
        return DecisionSummaryRecord(
            decision_cycle_id=cycle_id,
            consensus_direction=Direction.NEUTRAL,
            consensus_probability=Decimal("0.5"),
            consensus_confidence=Decimal("0"),
            long_weight=Decimal("0"),
            short_weight=Decimal("0"),
            neutral_weight=Decimal("0"),
            dissenters=(),
            dissent_probability=Decimal("0"),
            dissent_confidence=Decimal("0"),
            challenge_blocked=False,
            expected_gain=Decimal("0"),
            expected_loss=Decimal("0"),
            gross_ev=Decimal("0"),
            funding_carry=Decimal("0"),
            estimated_cost=Decimal("0"),
            net_ev=Decimal("0"),
            benchmark_return=Decimal("0"),
            alpha_estimate=Decimal("0"),
            consensus_snapshot={"direction": Direction.NEUTRAL, "reason": reason},
            adversarial_snapshot={"executed": False, "reason": reason},
            ev_snapshot={"executed": False, "reason": reason},
            cost_snapshot={"executed": False, "reason": reason},
        )

    def _matrix_records(
        self,
        command: OrchestrationCommand,
        cycle_id: UUID,
        matrix: AgentEvaluationMatrix,
    ) -> tuple[AgentEvaluationRecord, ...]:
        records: list[AgentEvaluationRecord] = []
        for evaluation in matrix.evaluations:
            reasons = self._record_reasons(evaluation.reason_codes)
            records.append(
                AgentEvaluationRecord(
                    id=_id("agent-evaluation", cycle_id, evaluation.agent_name),
                    decision_cycle_id=cycle_id,
                    agent_version_id=command.agent_version_ids[evaluation.agent_name],
                    agent_name=evaluation.agent_name,
                    compatible=evaluation.compatible,
                    enabled=evaluation.enabled,
                    weight=evaluation.weight,
                    direction=evaluation.output.direction,
                    probability=evaluation.output.probability,
                    confidence=evaluation.output.confidence,
                    risk=evaluation.output.risk,
                    input_snapshot=evaluation.input_contributions,
                    reason_codes=reasons,
                    explanation={
                        "voting": evaluation.voting,
                        "maturity": evaluation.maturity,
                        "proxy_label": evaluation.proxy_label,
                        "reason_codes": evaluation.reason_codes,
                        "failure_reason": evaluation.failure_reason,
                    },
                    duration_ms=evaluation.duration_ms,
                    created_at=command.evaluated_at,
                )
            )
        return tuple(records)

    @staticmethod
    def _record_reasons(values: tuple[str, ...]) -> tuple[ReasonCode, ...]:
        mapping = {
            "agent_evaluated": ReasonCode.ACCEPTED,
            "disabled_agent": ReasonCode.DISABLED_AGENT,
            "incompatible_regime": ReasonCode.INCOMPATIBLE_REGIME,
            "agent_exception": ReasonCode.AGENT_FAILED,
            "agent_missing": ReasonCode.AGENT_FAILED,
            "agent_duplicate": ReasonCode.AGENT_FAILED,
            "agent_registry_invalid": ReasonCode.AGENT_FAILED,
        }
        return tuple(mapping.get(value, ReasonCode.AGENT_FAILED) for value in values)

    @staticmethod
    def _gate(
        cycle_id: UUID,
        gate_type: GateType,
        sequence: int,
        passed: bool,
        reason: ReasonCode,
        *,
        input: Mapping[str, object],
        output: Mapping[str, object],
        command: OrchestrationCommand,
    ) -> GateEvaluationRecord:
        return GateEvaluationRecord(
            id=_id("gate-evaluation", cycle_id, sequence, gate_type),
            decision_cycle_id=cycle_id,
            gate_type=gate_type,
            sequence=sequence,
            passed=passed,
            reason_code=reason,
            input=_json_object(input),
            output=_json_object(output),
            evaluated_at=command.completed_at,
            duration_ms=0,
        )

    @staticmethod
    def _incident(
        command: OrchestrationCommand,
        *,
        component: str,
        reason: str,
        evidence: Mapping[str, object],
        severity: IncidentSeverity,
    ) -> IncidentState:
        deduplication_key = f"{component}:{reason}:{command.frame.frame_id}"
        return IncidentState.create(
            incident_id=_id("incident", command.manifest.experiment_id, deduplication_key),
            experiment_id=command.manifest.experiment_id,
            deduplication_key=deduplication_key,
            severity=severity,
            component=component,
            reason_code=reason,
            evidence=evidence,
            requires_operator_review=True,
            detected_at=command.completed_at,
        )
