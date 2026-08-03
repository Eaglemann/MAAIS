"""Weighted consensus aggregation of AgentOutputs.

Each agent output is weighted by its registry weight.
The direction with the highest weighted probability sum wins.
"""

from maais.agents.base import AgentOutput
from maais.decision.schemas import ConsensusResult
from maais.decision.weights import AgentWeightRegistry


def compute_consensus(
    outputs: list[AgentOutput],
    registry: AgentWeightRegistry,
) -> ConsensusResult:
    """Aggregate agent outputs into a single weighted consensus.

    Args:
        outputs: AgentOutput list from run_agents().
        registry: Weight registry (all agents start at 1.0).

    Returns:
        ConsensusResult with direction, weighted probability, and confidence.
    """
    if not outputs:
        return ConsensusResult(
            direction="neutral",
            weighted_probability=0.5,
            weighted_confidence=0.0,
            long_weight_sum=0.0,
            short_weight_sum=0.0,
            neutral_weight_sum=0.0,
            agents_counted=0,
            agents_long=[],
            agents_short=[],
            agents_neutral=[],
        )

    long_wp_sum = 0.0  # weighted probability sum for long direction
    short_wp_sum = 0.0
    long_wc_sum = 0.0  # weighted confidence sum
    short_wc_sum = 0.0
    long_w_sum = 0.0  # pure weight sum (for direction vote)
    short_w_sum = 0.0
    neutral_w_sum = 0.0
    agents_long: list[str] = []
    agents_short: list[str] = []
    agents_neutral: list[str] = []

    for out in outputs:
        w = registry.get(out.agent_name)
        if out.directional_hypothesis == "long":
            long_wp_sum += w * out.probability_estimate
            long_wc_sum += w * out.confidence_score
            long_w_sum += w
            agents_long.append(out.agent_name)
        elif out.directional_hypothesis == "short":
            short_wp_sum += w * out.probability_estimate
            short_wc_sum += w * out.confidence_score
            short_w_sum += w
            agents_short.append(out.agent_name)
        else:
            neutral_w_sum += w
            agents_neutral.append(out.agent_name)

    # Determine winning direction
    if long_w_sum == 0 and short_w_sum == 0:
        return ConsensusResult(
            direction="neutral",
            weighted_probability=0.5,
            weighted_confidence=0.0,
            long_weight_sum=0.0,
            short_weight_sum=0.0,
            neutral_weight_sum=neutral_w_sum,
            agents_counted=len(outputs),
            agents_long=agents_long,
            agents_short=agents_short,
            agents_neutral=agents_neutral,
        )

    if long_w_sum >= short_w_sum:
        direction = "long"
        weighted_prob = long_wp_sum / long_w_sum if long_w_sum > 0 else 0.5
        weighted_conf = long_wc_sum / long_w_sum if long_w_sum > 0 else 0.0
    else:
        direction = "short"
        weighted_prob = short_wp_sum / short_w_sum if short_w_sum > 0 else 0.5
        weighted_conf = short_wc_sum / short_w_sum if short_w_sum > 0 else 0.0

    return ConsensusResult(
        direction=direction,
        weighted_probability=weighted_prob,
        weighted_confidence=weighted_conf,
        long_weight_sum=long_w_sum,
        short_weight_sum=short_w_sum,
        neutral_weight_sum=neutral_w_sum,
        agents_counted=len(outputs),
        agents_long=agents_long,
        agents_short=agents_short,
        agents_neutral=agents_neutral,
    )
