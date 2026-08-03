from typing import NewType
from uuid import UUID, uuid4

ExperimentId = NewType("ExperimentId", UUID)
DecisionCycleId = NewType("DecisionCycleId", UUID)
MarketFrameId = NewType("MarketFrameId", UUID)
ProposalId = NewType("ProposalId", UUID)


def new_uuid() -> UUID:
    """Return a random UUIDv4 for an operational domain identity."""

    return uuid4()
