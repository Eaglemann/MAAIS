from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from maais.decisions.bundle import DecisionBundle
from maais.operations.incidents import IncidentState


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
