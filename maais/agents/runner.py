"""Agent runner — executes all 8 agents in parallel (asyncio.gather).

Architecture:
  1. Filter agents by regime compatibility (Rule 13)
  2. Run all compatible agents concurrently
  3. Return all AgentOutputs for the Decision Engine (Batch 5)

Open question resolved: parallel execution (asyncio.gather).
Sequential fallback available if needed.
"""

import asyncio

from maais.agents.base import AgentOutput, BaseAgent
from maais.agents.carry_yield import CarryYieldAgent
from maais.agents.liquidity import LiquidityAgent
from maais.agents.macro_sentiment import MacroSentimentAgent
from maais.agents.mean_reversion import MeanReversionAgent
from maais.agents.momentum import MomentumAgent
from maais.agents.order_flow_toxicity import OrderFlowToxicityAgent
from maais.agents.regime_gate import filter_compatible_agents
from maais.agents.stop_run_detection import StopRunDetectionAgent
from maais.agents.technical_structure import TechnicalStructureAgent
from maais.core.logging import get_logger
from maais.feature_pipeline.features import FeatureSet

logger = get_logger(__name__)


def build_agent_registry() -> list[BaseAgent]:
    """Instantiate all 8 agents. Call once at startup."""
    return [
        MomentumAgent(),
        TechnicalStructureAgent(),
        LiquidityAgent(),
        OrderFlowToxicityAgent(),
        StopRunDetectionAgent(),
        MeanReversionAgent(),
        CarryYieldAgent(),
        MacroSentimentAgent(),
    ]


async def run_agents(
    features: FeatureSet,
    agents: list[BaseAgent] | None = None,
) -> list[AgentOutput]:
    """Run all compatible agents in parallel and return their outputs.

    Args:
        features: The current FeatureSet for the symbol/timeframe.
        agents: Agent list (defaults to all 8 from build_agent_registry).

    Returns:
        List of AgentOutput from all compatible agents that completed.
        Agents incompatible with the current regime are silently skipped (Rule 13).
    """
    if agents is None:
        agents = build_agent_registry()

    compatible = filter_compatible_agents(agents, features.regime)

    logger.debug(
        "running_agents",
        total=len(agents),
        compatible=len(compatible),
        regime=features.regime,
        symbol=features.symbol,
    )

    results: list[AgentOutput] = list(
        await asyncio.gather(*[agent.analyze(features) for agent in compatible])
    )

    logger.debug(
        "agents_complete",
        symbol=features.symbol,
        outputs=len(results),
    )
    return results


def agent_names_ran(outputs: list[AgentOutput]) -> list[str]:
    return [o.agent_name for o in outputs]
