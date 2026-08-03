"""Order Flow Toxicity Agent — detects fragmented institutional order flow.

Toxic order flow: wide spread combined with strong book imbalance indicates
a large institutional participant pushing price directionally.
"""

from maais.agents.base import AgentOutput, BaseAgent, _clip, _neutral, _signal_to_output
from maais.config.constants import AgentName
from maais.feature_pipeline.features import FeatureSet

_NAME = AgentName.ORDER_FLOW_TOXICITY


class OrderFlowToxicityAgent(BaseAgent):
    name = _NAME
    compatible_regimes = ()  # compatible with all regimes

    async def analyze(self, features: FeatureSet) -> AgentOutput:
        if features.book_imbalance is None or features.bid_ask_spread is None:
            return _neutral(_NAME)

        imbalance = features.book_imbalance
        spread = features.bid_ask_spread

        # Toxicity = wide spread + strong imbalance → directional institutional flow
        toxicity = spread * abs(imbalance) * 100  # rough toxicity score
        direction_vote = imbalance * 2.0
        total = 2.0

        # Higher toxicity → higher confidence in direction but also higher risk
        confidence = _clip(toxicity)
        risk = _clip(toxicity + spread * 20)

        return _signal_to_output(_NAME, direction_vote, total, confidence, risk)
