"""Agent weight registry — initialized to 1.0, updated by Batch 9 learning engine.

Weights are stored in-memory during a session and persisted to PostgreSQL.
On startup, weights are loaded from the database (if available), falling back to
equal weights of 1.0.
"""

from __future__ import annotations

from maais.config.constants import AgentName
from maais.core.logging import get_logger

logger = get_logger(__name__)

# Default weight for all agents (equal initialization)
DEFAULT_WEIGHT = 1.0

# Canonical list of all agent names
_ALL_AGENT_NAMES: tuple[str, ...] = (
    AgentName.MOMENTUM,
    AgentName.TECHNICAL_STRUCTURE,
    AgentName.LIQUIDITY,
    AgentName.ORDER_FLOW_TOXICITY,
    AgentName.STOP_RUN_DETECTION,
    AgentName.MEAN_REVERSION,
    AgentName.CARRY_YIELD,
    AgentName.MACRO_SENTIMENT,
)


class AgentWeightRegistry:
    """In-memory agent weight store. Load from DB at startup; persist on update.

    All weights initialized to DEFAULT_WEIGHT (1.0).
    Batch 9 will call update_weight() after post-trade analysis.
    """

    def __init__(self) -> None:
        self._weights: dict[str, float] = {name: DEFAULT_WEIGHT for name in _ALL_AGENT_NAMES}

    def get(self, agent_name: str) -> float:
        """Return weight for an agent. Unknown agents get DEFAULT_WEIGHT."""
        return self._weights.get(agent_name, DEFAULT_WEIGHT)

    def update_weight(self, agent_name: str, new_weight: float) -> None:
        """Update a single agent's weight. Weight must be > 0."""
        if new_weight <= 0:
            raise ValueError(f"Weight must be positive, got {new_weight} for {agent_name}")
        old = self._weights.get(agent_name, DEFAULT_WEIGHT)
        self._weights[agent_name] = new_weight
        logger.info("weight_updated", agent=agent_name, old=old, new=new_weight)

    def all_weights(self) -> dict[str, float]:
        return dict(self._weights)

    def load_from_dict(self, weights: dict[str, float]) -> None:
        """Bulk load weights (e.g. from DB rows). Unknown keys are ignored."""
        for name, w in weights.items():
            if w > 0:
                self._weights[name] = w
        logger.info("weights_loaded", count=len(weights))

    def reset_to_defaults(self) -> None:
        """Reset all weights to 1.0."""
        self._weights = {name: DEFAULT_WEIGHT for name in _ALL_AGENT_NAMES}
        logger.info("weights_reset_to_defaults")
