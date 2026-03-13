"""Adversarial reasoning process — Rule 5 (BEHAVIOURS.md).

Agents on the minority side challenge the majority consensus.
In Phase 1 (no LLM), this is a structured numeric challenge:
  - If the minority has strong conviction (high weighted confidence),
    the challenge is flagged for review.
  - The LLM reasoning slot is reserved for Ollama integration.

The challenge_accepted flag blocks the trade gate when the minority
case is statistically strong enough to warrant caution.

Adversarial block threshold: minority weighted_confidence >= 0.65
(minority must be highly confident to override majority)
"""

from maais.agents.base import AgentOutput
from maais.decision.schemas import AdversarialSummary
from maais.decision.weights import AgentWeightRegistry

# Minority confidence threshold to block trade (Rule 5)
_ADVERSARIAL_BLOCK_THRESHOLD = 0.65


def run_adversarial(
    outputs: list[AgentOutput],
    majority_direction: str,
    registry: AgentWeightRegistry,
) -> AdversarialSummary:
    """Run the adversarial challenge step.

    Args:
        outputs: All agent outputs.
        majority_direction: The consensus direction ("long" | "short").
        registry: Weight registry.

    Returns:
        AdversarialSummary with dissenter analysis and challenge verdict.
    """
    if majority_direction == "neutral":
        return AdversarialSummary(
            majority_direction="neutral",
            minority_direction="neutral",
            dissenting_agents=[],
            dissenting_probability=0.5,
            dissenting_confidence=0.0,
            challenge_accepted=False,
        )

    minority_direction = "short" if majority_direction == "long" else "long"

    dissenters = [
        out for out in outputs
        if out.directional_hypothesis == minority_direction
    ]

    if not dissenters:
        return AdversarialSummary(
            majority_direction=majority_direction,
            minority_direction=minority_direction,
            dissenting_agents=[],
            dissenting_probability=0.5,
            dissenting_confidence=0.0,
            challenge_accepted=False,
        )

    # Weighted average of dissenting probability and confidence
    total_w = sum(registry.get(d.agent_name) for d in dissenters)
    if total_w == 0:
        avg_prob = 0.5
        avg_conf = 0.0
    else:
        avg_prob = sum(registry.get(d.agent_name) * d.probability_estimate for d in dissenters) / total_w
        avg_conf = sum(registry.get(d.agent_name) * d.confidence_score for d in dissenters) / total_w

    # Challenge is accepted if dissenters are highly confident
    challenge_accepted = avg_conf >= _ADVERSARIAL_BLOCK_THRESHOLD

    return AdversarialSummary(
        majority_direction=majority_direction,
        minority_direction=minority_direction,
        dissenting_agents=[d.agent_name for d in dissenters],
        dissenting_probability=avg_prob,
        dissenting_confidence=avg_conf,
        challenge_accepted=challenge_accepted,
        llm_reasoning=None,  # populated when Ollama is wired in
    )
