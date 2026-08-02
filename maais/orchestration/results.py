from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from maais.decisions.bundle import DecisionBundle
from maais.domain.enums import ProposalStatus
from maais.execution.paper.authorization import ExecutionCapability
from maais.execution.paper.records import PaperExecutionRecord
from maais.execution.paper.sensitivity import SensitivityOutcome, SensitivityScenario
from maais.operations.incidents import IncidentState
from maais.research.counterfactuals import CounterfactualState


class OrchestrationDisposition(StrEnum):
    QUARANTINED = "quarantined"
    HALTED = "halted"
    NEUTRAL = "neutral"
    REJECTED = "rejected"
    EXECUTED = "executed"


@dataclass(frozen=True, slots=True)
class OrchestrationOutcome:
    disposition: OrchestrationDisposition
    bundle: DecisionBundle
    incident: IncidentState | None
    counterfactual: CounterfactualState | None = None
    capability: ExecutionCapability | None = None
    execution: PaperExecutionRecord | None = None
    sensitivities: tuple[SensitivityOutcome, ...] = ()

    def __post_init__(self) -> None:
        self.bundle.validate()
        if self.incident is not None and (
            self.incident.experiment_id != self.bundle.cycle.experiment_id
        ):
            raise ValueError("incident and decision bundle experiment differ")
        if (
            self.disposition
            in {
                OrchestrationDisposition.QUARANTINED,
                OrchestrationDisposition.HALTED,
            }
            and self.incident is None
        ):
            raise ValueError("quarantined and halted outcomes require an incident")
        proposal = self.bundle.proposal
        if self.counterfactual is not None and (
            proposal is None
            or proposal.status is not ProposalStatus.REJECTED
            or self.counterfactual.proposal_id != proposal.id
        ):
            raise ValueError("counterfactual requires the matching rejected proposal")
        if self.capability is not None and (
            proposal is None or proposal.status is not ProposalStatus.APPROVED
        ):
            raise ValueError("execution capability requires an approved proposal")
        if self.execution is not None:
            self.execution.validate()
            if self.capability is None or proposal is None:
                raise ValueError("paper execution requires its approved capability")
        if self.sensitivities and (
            self.execution is None
            or len(self.sensitivities) != len(SensitivityScenario)
            or {item.scenario for item in self.sensitivities} != set(SensitivityScenario)
        ):
            raise ValueError("execution requires exactly three sensitivity scenarios")
        if self.disposition is OrchestrationDisposition.EXECUTED and (
            self.execution is None or self.incident is not None
        ):
            raise ValueError("executed outcome must have execution and no incident")
        if self.disposition is OrchestrationDisposition.REJECTED and (
            proposal is None
            or proposal.status is not ProposalStatus.REJECTED
            or self.counterfactual is None
        ):
            raise ValueError("directional rejection requires a proposal and counterfactual")
