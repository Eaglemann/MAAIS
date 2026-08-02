from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from maais.decision.official import (
    OfficialDecisionAnalysis,
    OfficialDecisionAnalytics,
    OfficialDecisionPolicy,
)
from maais.decisions.bundle import (
    AgentEvaluationRecord,
    DecisionBundle,
    DecisionCycleRecord,
    DecisionSummaryRecord,
    GateEvaluationRecord,
    MarketFrameRecord,
    TradeProposalRecord,
    record_to_dict,
)
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    PaperOrderSide,
    PaperOrderType,
    ProposalStatus,
    QualityStatus,
    ReasonCode,
)
from maais.domain.json import JsonValue, content_hash, freeze_json
from maais.execution.paper.authorization import (
    AuthorizationClaims,
    ExecutionAuthorizer,
)
from maais.execution.paper.broker import MarketEntryCommand, PaperBroker
from maais.execution.paper.filters import FilterRejection, PreparedOrder
from maais.execution.paper.sensitivity import calculate_sensitivities
from maais.feature_pipeline.features import FeatureSet
from maais.market_data.frames import CausalMinuteFrame
from maais.market_data.integrity.state_machine import FrameAdmission
from maais.monitoring.admission import (
    MonitoringAdmissionDecision,
    OfficialAdmissionPolicy,
    OfficialMonitoringAdmission,
)
from maais.operations.incidents import IncidentSeverity, IncidentState
from maais.orchestration.commands import OrchestrationCommand
from maais.orchestration.results import OrchestrationDisposition, OrchestrationOutcome
from maais.research.counterfactuals import CounterfactualState
from maais.risk.official import (
    OfficialRiskDecision,
    OfficialRiskEngine,
    OfficialRiskPolicy,
    OfficialRiskRequest,
    RiskCheck,
)


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


@dataclass(frozen=True, slots=True)
class _GateFact:
    gate_type: GateType
    raw_passed: bool
    reason_code: ReasonCode
    raw_reason: str
    input: Mapping[str, object]
    output: Mapping[str, object]


class OfficialOrchestrationService:
    """Pure fail-closed assembly of one authoritative decision outcome."""

    def __init__(
        self,
        feature_computer: FeatureComputer,
        *,
        agents: Sequence[BaseAgent] | None = None,
        durations: DurationSource | None = None,
        authorizer: ExecutionAuthorizer | None = None,
        paper_broker: PaperBroker | None = None,
        decision_analytics: OfficialDecisionAnalytics | None = None,
        monitoring_admission: OfficialMonitoringAdmission | None = None,
        risk_engine: OfficialRiskEngine | None = None,
    ) -> None:
        self._feature_computer = feature_computer
        self._agents = tuple(agents) if agents is not None else None
        self._durations = durations
        self._authorizer = authorizer
        self._paper_broker = paper_broker
        self._decision_analytics = decision_analytics or OfficialDecisionAnalytics(
            OfficialDecisionPolicy.conservative()
        )
        self._monitoring_admission = monitoring_admission or OfficialMonitoringAdmission(
            OfficialAdmissionPolicy.conservative()
        )
        self._risk_engine = risk_engine or OfficialRiskEngine(OfficialRiskPolicy.conservative())

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
        return self._admitted(command, features, matrix)

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

    def _admitted(
        self,
        command: OrchestrationCommand,
        features: FeatureSet,
        matrix: AgentEvaluationMatrix,
    ) -> OrchestrationOutcome:
        cycle_id = self._cycle_id(command)
        executable_price = self._executable_price(command)
        context = command.entry_context
        benchmark = context.monitoring.benchmark if context is not None else None
        analysis = self._decision_analytics.evaluate(
            features=features,
            matrix=matrix,
            executable_price=executable_price,
            benchmark_return=benchmark.return_fraction if benchmark is not None else None,
        )
        if analysis.direction is Direction.NEUTRAL:
            facts = self._gate_facts(
                command,
                matrix,
                analysis,
                monitoring=None,
                risk=None,
                prepared=None,
                filter_reason="direction_is_neutral",
            )
            gates = self._gate_records(command, cycle_id, facts)
            bundle = DecisionBundle(
                market_frame=self._market_frame(command),
                cycle=self._cycle(
                    command,
                    cycle_id=cycle_id,
                    status=DecisionStatus.COMPLETED,
                    reason=ReasonCode.NEUTRAL_CONSENSUS,
                    regime=features.regime or "unknown",
                    feature_snapshot=features.to_dict(),
                    direction=Direction.NEUTRAL,
                    disposition=Disposition.NEUTRAL,
                ),
                agents=self._matrix_records(command, cycle_id, matrix),
                summary=self._analysis_summary(cycle_id, analysis),
                gates=gates,
                proposal=None,
            )
            return OrchestrationOutcome(OrchestrationDisposition.NEUTRAL, bundle, None)

        if context is None:
            return self._neutral_halt(
                command,
                reason_code=ReasonCode.MONITORING_UNHEALTHY,
                component="orchestration",
                incident_reason="entry_decision_context_missing",
                feature_snapshot=features.to_dict(),
            )

        monitoring = self._monitoring_admission.evaluate(context.monitoring)
        risk = self._risk_decision(command, analysis, executable_price)
        prepared: PreparedOrder | None = None
        filter_reason = "risk_quantity_unavailable"
        if risk is not None and risk.quantity > 0:
            try:
                prepared = context.exchange_filters.prepare(
                    side=self._side(analysis.direction),
                    order_type=PaperOrderType.MARKET,
                    requested_quantity=risk.quantity,
                    approved_quantity=risk.quantity,
                    price=executable_price,
                    approved_notional=risk.notional,
                )
                filter_reason = "exchange_filters_passed"
            except FilterRejection as exc:
                filter_reason = exc.reason

        facts = self._gate_facts(
            command,
            matrix,
            analysis,
            monitoring=monitoring,
            risk=risk,
            prepared=prepared,
            filter_reason=filter_reason,
        )
        gates = self._gate_records(command, cycle_id, facts)
        all_gates_passed = all(gate.passed for gate in gates)
        rejection_index = next(
            (index for index, gate in enumerate(gates) if not gate.passed),
            None,
        )
        rejection_gate = gates[rejection_index].gate_type if rejection_index is not None else None
        final_reason = (
            ReasonCode.ACCEPTED if all_gates_passed else gates[rejection_index].reason_code  # type: ignore[index]
        )
        proposal_id = _id("trade-proposal", cycle_id)
        gate_chain_hash = content_hash([record_to_dict(gate) for gate in gates])
        proposed_quantity = (
            prepared.quantity
            if prepared is not None
            else self._research_quantity(command, executable_price)
        )
        proposed_notional = proposed_quantity * executable_price
        expires_at = command.completed_at + context.proposal_ttl
        exit_loss = self._research_loss(analysis)
        exit_gain = self._research_gain(analysis)
        stop_price = self._stop_price(
            analysis.direction,
            executable_price,
            exit_loss,
        )
        target_price = self._target_price(
            analysis.direction,
            executable_price,
            exit_gain,
        )
        proposal = TradeProposalRecord(
            id=proposal_id,
            decision_cycle_id=cycle_id,
            experiment_id=command.manifest.experiment_id,
            symbol=command.frame.key.symbol,
            direction=analysis.direction,
            status=(ProposalStatus.APPROVED if all_gates_passed else ProposalStatus.REJECTED),
            reason_code=final_reason,
            proposed_at=command.completed_at,
            expires_at=expires_at,
            entry_policy=_json_object(
                {
                    "order_type": PaperOrderType.MARKET,
                    "executable_price": executable_price,
                    "side": self._side(analysis.direction),
                    "gate_chain_hash": gate_chain_hash,
                    "filter_captured_at": context.exchange_filters.captured_at,
                }
            ),
            exit_policy=_json_object(
                {
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "expected_loss_fraction": exit_loss,
                    "expected_gain_fraction": exit_gain,
                    "expected_move_source": (
                        "atr" if analysis.expected_loss > 0 else "research_only_fallback"
                    ),
                }
            ),
            sizing_snapshot=_json_object(
                {
                    "risk_decision_hash": risk.content_hash if risk is not None else None,
                    "risk_input": risk.input_snapshot if risk is not None else None,
                    "risk_gates": (
                        [item.to_dict() for item in risk.gates] if risk is not None else []
                    ),
                    "prepared_quantity": prepared.quantity if prepared is not None else None,
                    "research_quantity": proposed_quantity,
                }
            ),
            approved_quantity=proposed_quantity if all_gates_passed else None,
            approved_notional=proposed_notional if all_gates_passed else None,
            risk_at_stop=(
                risk.risk_at_stop
                if all_gates_passed and risk is not None and risk.risk_at_stop > 0
                else None
            ),
        )
        bundle = DecisionBundle(
            market_frame=self._market_frame(command),
            cycle=self._cycle(
                command,
                cycle_id=cycle_id,
                status=(DecisionStatus.COMPLETED if all_gates_passed else DecisionStatus.REJECTED),
                reason=final_reason,
                regime=features.regime or "unknown",
                feature_snapshot=features.to_dict(),
                direction=analysis.direction,
                disposition=(Disposition.APPROVED if all_gates_passed else Disposition.REJECTED),
            ),
            agents=self._matrix_records(command, cycle_id, matrix),
            summary=self._analysis_summary(cycle_id, analysis),
            gates=gates,
            proposal=proposal,
        )
        if not all_gates_passed:
            if rejection_gate is None or rejection_index is None:
                raise RuntimeError("rejected directional proposal has no rejection gate")
            counterfactual = CounterfactualState.create(
                counterfactual_id=_id("counterfactual", proposal_id),
                experiment_id=command.manifest.experiment_id,
                proposal_id=proposal_id,
                decision_cycle_id=cycle_id,
                symbol=command.frame.key.symbol,
                direction=analysis.direction,
                rejection_gate=rejection_gate,
                prior_gate_chain=tuple(gate.gate_type for gate in gates[: rejection_index + 1]),
                quantity=proposed_quantity,
                decision_executable_price=executable_price,
                eligible_after=command.completed_at + context.execution_latency,
                fee_rate=context.taker_fee_rate,
                expected_loss_fraction=exit_loss,
                expected_gain_fraction=exit_gain,
                created_at=command.completed_at,
            )
            return OrchestrationOutcome(
                OrchestrationDisposition.REJECTED,
                bundle,
                None,
                counterfactual=counterfactual,
            )

        if prepared is None or self._authorizer is None or self._paper_broker is None:
            raise RuntimeError("passed broker-capacity gate lacks execution dependencies")
        claims = AuthorizationClaims(
            experiment_id=command.manifest.experiment_id,
            decision_cycle_id=cycle_id,
            proposal_id=proposal_id,
            gate_chain_hash=gate_chain_hash,
            symbol=command.frame.key.symbol,
            side=self._side(analysis.direction),
            quantity=prepared.quantity,
            approved_notional=proposed_notional,
            issued_at=command.completed_at,
            expires_at=expires_at,
        )
        capability = self._authorizer.issue(claims, all_gates_passed=True)
        position_id = (
            context.account.position(command.frame.key.symbol).position_id
            if command.frame.key.symbol in context.account.positions
            else _id("paper-position", command.manifest.experiment_id, command.frame.key.symbol)
        )
        entry_command = MarketEntryCommand(
            order_id=_id("paper-order", proposal_id, "entry"),
            fill_id=_id("paper-fill", proposal_id, "entry"),
            position_id=position_id,
            exit_plan_id=_id("exit-plan", position_id),
            experiment_id=command.manifest.experiment_id,
            decision_cycle_id=cycle_id,
            proposal_id=proposal_id,
            gate_chain_hash=gate_chain_hash,
            client_order_id=f"paper-{proposal_id}",
            symbol=command.frame.key.symbol,
            side=self._side(analysis.direction),
            requested_quantity=prepared.quantity,
            approved_quantity=prepared.quantity,
            approved_notional=proposed_notional,
            decision_executable_price=executable_price,
            decision_completed_at=command.completed_at,
            execution_latency=context.execution_latency,
            created_at=command.completed_at,
            expires_at=expires_at,
            taker_fee_rate=context.taker_fee_rate,
            expected_loss_fraction=exit_loss,
            expected_gain_fraction=exit_gain,
            capability=capability,
            exchange_filters=context.exchange_filters,
        )
        try:
            result = self._paper_broker.execute_market_entry(
                entry_command,
                account=context.account,
                books=context.books,
                active_exit_plan=context.active_exit_plan,
            )
            sensitivities = calculate_sensitivities(
                result.fill,
                marked_price=result.fill.book.mark_price,
                calculated_at=result.fill.fill_at,
            )
        except Exception as exc:
            incident = self._incident(
                command,
                component="paper_execution",
                reason="approved_entry_execution_failed",
                evidence={
                    "decision_cycle_id": cycle_id,
                    "proposal_id": proposal_id,
                    "gate_chain_hash": gate_chain_hash,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                severity=IncidentSeverity.CRITICAL,
            )
            return OrchestrationOutcome(
                OrchestrationDisposition.HALTED,
                bundle,
                incident,
                capability=capability,
            )
        return OrchestrationOutcome(
            OrchestrationDisposition.EXECUTED,
            bundle,
            None,
            capability=capability,
            execution=result.record,
            sensitivities=sensitivities,
        )

    @staticmethod
    def _executable_price(command: OrchestrationCommand) -> Decimal:
        frame = command.frame
        if frame.mark_price is not None and frame.mark_price > 0:
            return frame.mark_price
        if (
            frame.best_bid is not None
            and frame.best_ask is not None
            and frame.best_bid > 0
            and frame.best_ask > frame.best_bid
        ):
            return (frame.best_bid + frame.best_ask) / Decimal("2")
        raise ValueError("admitted frame has no executable mark or midpoint")

    def _risk_decision(
        self,
        command: OrchestrationCommand,
        analysis: OfficialDecisionAnalysis,
        executable_price: Decimal,
    ) -> OfficialRiskDecision | None:
        context = command.entry_context
        if context is None or analysis.expected_loss <= 0 or analysis.expected_gain <= 0:
            return None
        stop_price = self._stop_price(
            analysis.direction,
            executable_price,
            analysis.expected_loss,
        )
        return self._risk_engine.evaluate(
            OfficialRiskRequest(
                symbol=command.frame.key.symbol,
                direction=analysis.direction,
                capital=context.account.equity,
                executable_price=executable_price,
                stop_price=stop_price,
                p_win=analysis.consensus_probability,
                expected_gain_fraction=analysis.expected_gain,
                expected_loss_fraction=analysis.expected_loss,
                leverage=context.account.leverage,
                drawdown=context.drawdown,
                open_positions=context.open_positions,
                correlations=context.correlations,
                evaluated_at=command.evaluated_at,
            )
        )

    def _gate_facts(
        self,
        command: OrchestrationCommand,
        matrix: AgentEvaluationMatrix,
        analysis: OfficialDecisionAnalysis,
        *,
        monitoring: MonitoringAdmissionDecision | None,
        risk: OfficialRiskDecision | None,
        prepared: PreparedOrder | None,
        filter_reason: str,
    ) -> tuple[_GateFact, ...]:
        context = command.entry_context
        voting = tuple(item.agent_name for item in matrix.evaluations if item.voting)
        monitoring_reason = self._monitoring_reason(monitoring)
        drawdown_passed, drawdown_reason = self._risk_check(risk, RiskCheck.DRAWDOWN)
        correlation_passed, correlation_reason = self._risk_check(risk, RiskCheck.CORRELATION)
        portfolio_checks = (
            RiskCheck.KELLY,
            RiskCheck.PRICE_AND_STOP,
            RiskCheck.TRADE_RISK_AT_STOP,
            RiskCheck.PORTFOLIO_LOSS_AT_STOP,
            RiskCheck.GROSS_NOTIONAL,
        )
        portfolio_passed = risk is not None and all(
            self._risk_check(risk, check)[0] for check in portfolio_checks
        )
        portfolio_reason = self._first_risk_reason(risk, portfolio_checks)
        margin_passed, margin_reason = self._risk_check(risk, RiskCheck.MARGIN)
        leverage_passed = context is not None and context.account.leverage == 1 and margin_passed
        leverage_reason = (
            "leverage_and_margin_within_limit"
            if leverage_passed
            else (
                "paper_leverage_above_conservative_limit"
                if context is not None and context.account.leverage != 1
                else margin_reason
            )
        )
        exchange_passed = prepared is not None
        execution_dependencies_present = (
            self._authorizer is not None and self._paper_broker is not None
        )
        if context is None or prepared is None:
            capacity_passed = False
            capacity_reason = "prepared_order_unavailable"
            required_capacity = Decimal("0")
            free_margin = context.account.free_margin if context is not None else Decimal("0")
        else:
            required_capacity = (
                prepared.notional / Decimal(context.account.leverage)
                + prepared.notional * context.taker_fee_rate
            )
            free_margin = context.account.free_margin
            existing_position = context.account.positions.get(command.frame.key.symbol)
            position_direction_compatible = (
                existing_position is None
                or existing_position.is_flat
                or existing_position.side is analysis.direction
            )
            capacity_passed = (
                execution_dependencies_present
                and bool(context.books)
                and position_direction_compatible
                and required_capacity <= free_margin
            )
            if not execution_dependencies_present:
                capacity_reason = "paper_execution_dependency_missing"
            elif not context.books:
                capacity_reason = "eligible_book_stream_missing"
            elif not position_direction_compatible:
                capacity_reason = "existing_position_direction_conflict"
            elif required_capacity > free_margin:
                capacity_reason = "paper_account_capacity_exceeded"
            else:
                capacity_reason = "paper_broker_capacity_available"
        return (
            _GateFact(
                GateType.DATA_QUALITY,
                True,
                ReasonCode.ACCEPTED,
                "frame_admitted",
                {"integrity_hash": command.integrity.content_hash},
                {"admission": command.integrity.admission},
            ),
            _GateFact(
                GateType.REGIME_COMPATIBILITY,
                bool(voting),
                ReasonCode.ACCEPTED if voting else ReasonCode.INCOMPATIBLE_REGIME,
                "compatible_voting_agents_present" if voting else "no_compatible_voting_agents",
                {"regime": matrix.regime, "matrix_hash": matrix.content_hash},
                {"voting_agents": voting},
            ),
            _GateFact(
                GateType.CONSENSUS,
                analysis.consensus_passed,
                (
                    ReasonCode.ACCEPTED
                    if analysis.consensus_passed
                    else ReasonCode.NEUTRAL_CONSENSUS
                ),
                analysis.consensus_reason,
                analysis.consensus_snapshot,
                {"direction": analysis.direction},
            ),
            _GateFact(
                GateType.ADVERSARIAL,
                not analysis.challenge_blocked,
                (
                    ReasonCode.ADVERSARIAL_BLOCKED
                    if analysis.challenge_blocked
                    else ReasonCode.ACCEPTED
                ),
                "adversarial_challenge_blocked"
                if analysis.challenge_blocked
                else "adversarial_challenge_clear",
                analysis.adversarial_snapshot,
                {"challenge_blocked": analysis.challenge_blocked},
            ),
            _GateFact(
                GateType.EV,
                analysis.ev_positive,
                ReasonCode.ACCEPTED if analysis.ev_positive else ReasonCode.NON_POSITIVE_EV,
                analysis.ev_reason,
                analysis.ev_snapshot,
                {"net_ev": analysis.net_ev},
            ),
            _GateFact(
                GateType.ALPHA,
                analysis.alpha_positive,
                ReasonCode.ACCEPTED if analysis.alpha_positive else ReasonCode.ALPHA_FAILED,
                analysis.alpha_reason,
                analysis.ev_snapshot,
                {
                    "benchmark_available": analysis.benchmark_available,
                    "benchmark_return": analysis.benchmark_return,
                    "alpha_estimate": analysis.alpha_estimate,
                },
            ),
            _GateFact(
                GateType.MONITORING,
                monitoring is not None and monitoring.allowed,
                (
                    ReasonCode.ACCEPTED
                    if monitoring is not None and monitoring.allowed
                    else ReasonCode.MONITORING_UNHEALTHY
                ),
                monitoring_reason,
                monitoring.input_snapshot if monitoring is not None else {},
                {
                    "decision_hash": monitoring.content_hash if monitoring is not None else None,
                    "gates": (
                        [item.to_dict() for item in monitoring.gates]
                        if monitoring is not None
                        else []
                    ),
                },
            ),
            _GateFact(
                GateType.DRAWDOWN,
                drawdown_passed,
                ReasonCode.ACCEPTED if drawdown_passed else ReasonCode.DRAWDOWN_HALT,
                drawdown_reason,
                risk.input_snapshot if risk is not None else {},
                self._risk_gate_output(risk, RiskCheck.DRAWDOWN),
            ),
            _GateFact(
                GateType.CORRELATION,
                correlation_passed,
                ReasonCode.ACCEPTED if correlation_passed else ReasonCode.CORRELATION_BLOCKED,
                correlation_reason,
                risk.input_snapshot if risk is not None else {},
                self._risk_gate_output(risk, RiskCheck.CORRELATION),
            ),
            _GateFact(
                GateType.PORTFOLIO_RISK,
                portfolio_passed,
                (ReasonCode.ACCEPTED if portfolio_passed else ReasonCode.PORTFOLIO_RISK_EXCEEDED),
                portfolio_reason,
                risk.input_snapshot if risk is not None else {},
                {
                    "risk_decision_hash": risk.content_hash if risk is not None else None,
                    "gates": ([item.to_dict() for item in risk.gates] if risk is not None else []),
                },
            ),
            _GateFact(
                GateType.LEVERAGE,
                leverage_passed,
                ReasonCode.ACCEPTED if leverage_passed else ReasonCode.LEVERAGE_REJECTED,
                leverage_reason,
                {
                    "account_leverage": context.account.leverage if context is not None else None,
                    "conservative_limit": 1,
                },
                self._risk_gate_output(risk, RiskCheck.MARGIN),
            ),
            _GateFact(
                GateType.EXCHANGE_FILTERS,
                exchange_passed,
                (ReasonCode.ACCEPTED if exchange_passed else ReasonCode.EXCHANGE_FILTER_REJECTED),
                filter_reason,
                {
                    "filter_snapshot": (
                        {
                            "symbol": context.exchange_filters.symbol,
                            "status": context.exchange_filters.status,
                            "captured_at": context.exchange_filters.captured_at,
                            "quantity_step": context.exchange_filters.quantity_step,
                            "minimum_quantity": context.exchange_filters.minimum_quantity,
                            "maximum_quantity": context.exchange_filters.maximum_quantity,
                            "minimum_notional": context.exchange_filters.minimum_notional,
                        }
                        if context is not None
                        else None
                    )
                },
                {
                    "quantity": prepared.quantity if prepared is not None else None,
                    "notional": prepared.notional if prepared is not None else None,
                },
            ),
            _GateFact(
                GateType.PAPER_BROKER_CAPACITY,
                capacity_passed,
                (ReasonCode.ACCEPTED if capacity_passed else ReasonCode.BROKER_CAPACITY_REJECTED),
                capacity_reason,
                {
                    "required_capacity": required_capacity,
                    "free_margin": free_margin,
                    "execution_dependencies_present": execution_dependencies_present,
                    "eligible_books": len(context.books) if context is not None else 0,
                },
                {"capacity_available": capacity_passed},
            ),
        )

    def _gate_records(
        self,
        command: OrchestrationCommand,
        cycle_id: UUID,
        facts: tuple[_GateFact, ...],
    ) -> tuple[GateEvaluationRecord, ...]:
        if tuple(item.gate_type for item in facts) != tuple(GateType):
            raise ValueError("official orchestration must evaluate every ordered gate")
        records: list[GateEvaluationRecord] = []
        blocking_gate: GateType | None = None
        for sequence, fact in enumerate(facts, start=1):
            passed = blocking_gate is None and fact.raw_passed
            output = {
                **fact.output,
                "raw_passed": fact.raw_passed,
                "raw_reason": fact.raw_reason,
                "effective_status": (
                    "passed"
                    if passed
                    else ("failed" if blocking_gate is None else "not_applicable")
                ),
                "blocking_gate": blocking_gate,
            }
            records.append(
                self._gate(
                    cycle_id,
                    fact.gate_type,
                    sequence,
                    passed,
                    (
                        ReasonCode.PRIOR_GATE_FAILED
                        if blocking_gate is not None
                        else fact.reason_code
                    ),
                    input=fact.input,
                    output=output,
                    command=command,
                )
            )
            if blocking_gate is None and not fact.raw_passed:
                blocking_gate = fact.gate_type
        return tuple(records)

    @staticmethod
    def _analysis_summary(
        cycle_id: UUID,
        analysis: OfficialDecisionAnalysis,
    ) -> DecisionSummaryRecord:
        return DecisionSummaryRecord(
            decision_cycle_id=cycle_id,
            consensus_direction=analysis.direction,
            consensus_probability=analysis.consensus_probability,
            consensus_confidence=analysis.consensus_confidence,
            long_weight=analysis.long_weight,
            short_weight=analysis.short_weight,
            neutral_weight=analysis.neutral_weight,
            dissenters=analysis.dissenters,
            dissent_probability=analysis.dissent_probability,
            dissent_confidence=analysis.dissent_confidence,
            challenge_blocked=analysis.challenge_blocked,
            expected_gain=analysis.expected_gain,
            expected_loss=analysis.expected_loss,
            gross_ev=analysis.gross_ev,
            funding_carry=analysis.funding_carry,
            estimated_cost=analysis.estimated_cost,
            net_ev=analysis.net_ev,
            benchmark_return=analysis.benchmark_return,
            alpha_estimate=analysis.alpha_estimate,
            consensus_snapshot=analysis.consensus_snapshot,
            adversarial_snapshot=analysis.adversarial_snapshot,
            ev_snapshot=analysis.ev_snapshot,
            cost_snapshot=analysis.cost_snapshot,
        )

    @staticmethod
    def _risk_check(
        decision: OfficialRiskDecision | None,
        check: RiskCheck,
    ) -> tuple[bool, str]:
        if decision is None:
            return False, "risk_decision_unavailable"
        gate = next(item for item in decision.gates if item.check is check)
        return gate.status is QualityStatus.PASSED, gate.reason_code

    @classmethod
    def _first_risk_reason(
        cls,
        decision: OfficialRiskDecision | None,
        checks: tuple[RiskCheck, ...],
    ) -> str:
        for check in checks:
            passed, reason = cls._risk_check(decision, check)
            if not passed:
                return reason
        return "portfolio_risk_within_limit"

    @staticmethod
    def _risk_gate_output(
        decision: OfficialRiskDecision | None,
        check: RiskCheck,
    ) -> Mapping[str, object]:
        if decision is None:
            return {"status": QualityStatus.NOT_APPLICABLE, "reason": "risk_unavailable"}
        gate = next(item for item in decision.gates if item.check is check)
        return {
            "status": gate.status,
            "reason": gate.reason_code,
            "details": gate.details,
            "risk_decision_hash": decision.content_hash,
        }

    @staticmethod
    def _monitoring_reason(decision: MonitoringAdmissionDecision | None) -> str:
        if decision is None:
            return "monitoring_context_unavailable"
        first = next(
            (item for item in decision.gates if item.status is not QualityStatus.PASSED),
            None,
        )
        return first.reason_code if first is not None else "monitoring_admitted"

    @staticmethod
    def _side(direction: Direction) -> PaperOrderSide:
        if direction is Direction.LONG:
            return PaperOrderSide.BUY
        if direction is Direction.SHORT:
            return PaperOrderSide.SELL
        raise ValueError("neutral direction has no paper order side")

    @staticmethod
    def _stop_price(
        direction: Direction,
        executable_price: Decimal,
        loss_fraction: Decimal,
    ) -> Decimal:
        return (
            executable_price * (Decimal("1") - loss_fraction)
            if direction is Direction.LONG
            else executable_price * (Decimal("1") + loss_fraction)
        )

    @staticmethod
    def _target_price(
        direction: Direction,
        executable_price: Decimal,
        gain_fraction: Decimal,
    ) -> Decimal:
        return (
            executable_price * (Decimal("1") + gain_fraction)
            if direction is Direction.LONG
            else executable_price * (Decimal("1") - gain_fraction)
        )

    @staticmethod
    def _research_loss(analysis: OfficialDecisionAnalysis) -> Decimal:
        value = analysis.expected_loss if analysis.expected_loss > 0 else Decimal("0.01")
        return value.quantize(Decimal("0.000000000000000001"))

    @staticmethod
    def _research_gain(analysis: OfficialDecisionAnalysis) -> Decimal:
        value = analysis.expected_gain if analysis.expected_gain > 0 else Decimal("0.01")
        return value.quantize(Decimal("0.000000000000000001"))

    @staticmethod
    def _research_quantity(
        command: OrchestrationCommand,
        executable_price: Decimal,
    ) -> Decimal:
        context = command.entry_context
        if context is None:
            raise ValueError("research quantity requires an entry context")
        filters = context.exchange_filters
        target_notional = max(
            filters.minimum_notional,
            context.account.equity * Decimal("0.001"),
        )
        raw = max(filters.minimum_quantity, target_notional / executable_price)
        raw = min(raw, filters.maximum_quantity)
        quantity = filters.quantize_quantity(raw)
        if quantity <= 0:
            quantity = filters.minimum_quantity
        return quantity

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
            index_price=frame.index_price,
            funding_rate=frame.funding_rate,
            primary_spot_price=frame.primary_spot_price,
            secondary_venue_price=frame.secondary_venue_price,
            bar_snapshot=_json_object(frame.bar.to_dict()),
            orderbook_snapshot=_json_object(
                {
                    "bids": [level.to_dict() for level in frame.book_bids],
                    "asks": [level.to_dict() for level in frame.book_asks],
                    "best_bid": frame.best_bid,
                    "best_ask": frame.best_ask,
                    "source_event_id": (book_source.event_id if book_source is not None else None),
                    "source_content_hash": (
                        book_source.content_hash if book_source is not None else None
                    ),
                }
            ),
            source_manifest=_json_object(
                {name: source.to_dict() for name, source in sorted(frame.source_manifest.items())}
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
        direction: Direction = Direction.NEUTRAL,
        disposition: Disposition = Disposition.NEUTRAL,
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
            direction=direction,
            disposition=disposition,
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
