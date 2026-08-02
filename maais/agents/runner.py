"""Agent runner — executes all 8 agents in parallel (asyncio.gather).

Compatibility adapter for the original AgentOutput API.

The authoritative path is ``maais.agents.evaluations.run_agent_matrix``. This
adapter preserves one visible output per supplied agent and never silently
omits a regime-incompatible agent.

Execution remains concurrent through ``asyncio.gather``.
"""

import asyncio

from maais.agents.base import AgentOutput, BaseAgent, _neutral
from maais.agents.carry_yield import CarryYieldAgent
from maais.agents.liquidity import LiquidityAgent
from maais.agents.macro_sentiment import MacroSentimentAgent
from maais.agents.mean_reversion import MeanReversionAgent
from maais.agents.momentum import MomentumAgent
from maais.agents.order_flow_toxicity import OrderFlowToxicityAgent
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
    """Run supplied agents while retaining neutral incompatible outputs.

    Args:
        features: The current FeatureSet for the symbol/timeframe.
        agents: Agent list (defaults to all 8 from build_agent_registry).

    Returns:
        One AgentOutput per supplied agent. For the exact eight-row official
        contract, maturity metadata, deterministic timing, and failure rows,
        use ``run_agent_matrix``.
    """
    if agents is None:
        agents = build_agent_registry()

    logger.debug(
        "running_agents",
        total=len(agents),
        compatible=sum(agent.is_compatible_with_regime(features.regime) for agent in agents),
        regime=features.regime,
        symbol=features.symbol,
    )

    async def evaluate(agent: BaseAgent) -> AgentOutput:
        if not agent.is_compatible_with_regime(features.regime):
            return _neutral(agent.name)
        return await agent.analyze(features)

    results: list[AgentOutput] = list(await asyncio.gather(*(evaluate(agent) for agent in agents)))

    logger.debug(
        "agents_complete",
        symbol=features.symbol,
        outputs=len(results),
    )
    return results


def agent_names_ran(outputs: list[AgentOutput]) -> list[str]:
    return [o.agent_name for o in outputs]
