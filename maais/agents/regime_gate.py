"""Regime compatibility map — Rule 13 (BEHAVIOURS.md).

Strategies activate only in compatible regimes.
Each agent class declares compatible_regimes; this module
provides a helper to filter a list of agents.
"""

from maais.agents.base import BaseAgent, AgentOutput
from maais.feature_pipeline.features import FeatureSet


def filter_compatible_agents(agents: list[BaseAgent], regime: str | None) -> list[BaseAgent]:
    """Return only agents that are compatible with the current regime."""
    return [a for a in agents if a.is_compatible_with_regime(regime)]
