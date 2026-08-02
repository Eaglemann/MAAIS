"""Tests for the Strategy Agent Engine (Batch 4)."""

from datetime import datetime, timezone

import pytest

from maais.agents.base import (
    AgentOutput,
    _clip,
    _neutral,
    _signal_to_output,
    _votes_to_probability,
)
from maais.agents.carry_yield import CarryYieldAgent
from maais.agents.liquidity import LiquidityAgent
from maais.agents.macro_sentiment import MacroSentimentAgent
from maais.agents.mean_reversion import MeanReversionAgent
from maais.agents.momentum import MomentumAgent
from maais.agents.order_flow_toxicity import OrderFlowToxicityAgent
from maais.agents.regime_gate import filter_compatible_agents
from maais.agents.runner import agent_names_ran, build_agent_registry, run_agents
from maais.agents.stop_run_detection import StopRunDetectionAgent
from maais.agents.technical_structure import TechnicalStructureAgent
from maais.config.constants import AgentName, Regime
from maais.feature_pipeline.features import FeatureSet

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _features(
    regime: str = Regime.TRENDING,
    zscore: float | None = None,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    ema_signal: float | None = None,
    roc_short: float | None = None,
    roc_long: float | None = None,
    atr: float | None = None,
    rolling_std: float | None = None,
    bid_ask_spread: float | None = None,
    book_imbalance: float | None = None,
    funding_rate: float | None = None,
    annualized_funding: float | None = None,
    funding_bias: str | None = None,
    zscore_mean: float | None = None,
    zscore_std: float | None = None,
) -> FeatureSet:
    return FeatureSet(
        symbol="BTCUSDT",
        timeframe="1m",
        timestamp=_ts(),
        regime=regime,
        zscore=zscore,
        zscore_mean=zscore_mean,
        zscore_std=zscore_std,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        ema_signal=ema_signal,
        roc_short=roc_short,
        roc_long=roc_long,
        atr=atr,
        rolling_std=rolling_std,
        bid_ask_spread=bid_ask_spread,
        book_imbalance=book_imbalance,
        funding_rate=funding_rate,
        annualized_funding=annualized_funding,
        funding_bias=funding_bias,
    )


def _assert_valid_output(output: AgentOutput) -> None:
    """Assert all Rule 3 constraints."""
    assert output.directional_hypothesis in ("long", "short", "neutral")
    assert 0.0 <= output.probability_estimate <= 1.0
    assert 0.0 <= output.confidence_score <= 1.0
    assert 0.0 <= output.risk_estimate <= 1.0


# ── AgentOutput dataclass ─────────────────────────────────────────────────────


class TestAgentOutput:
    def test_valid_output_creates_without_error(self):
        out = AgentOutput("test", "long", 0.7, 0.8, 0.3)
        assert out.directional_hypothesis == "long"

    def test_invalid_hypothesis_raises(self):
        with pytest.raises(AssertionError):
            AgentOutput("test", "up", 0.7, 0.8, 0.3)

    def test_probability_out_of_range_raises(self):
        with pytest.raises(AssertionError):
            AgentOutput("test", "long", 1.5, 0.8, 0.3)

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(AssertionError):
            AgentOutput("test", "long", 0.7, -0.1, 0.3)

    def test_risk_out_of_range_raises(self):
        with pytest.raises(AssertionError):
            AgentOutput("test", "long", 0.7, 0.8, 1.5)

    def test_to_dict_has_all_keys(self):
        out = AgentOutput("test", "short", 0.6, 0.5, 0.4)
        d = out.to_dict()
        assert set(d.keys()) == {
            "agent_name",
            "directional_hypothesis",
            "probability_estimate",
            "confidence_score",
            "risk_estimate",
        }

    def test_neutral_output_helper(self):
        out = _neutral("test_agent")
        assert out.directional_hypothesis == "neutral"
        assert out.probability_estimate == 0.5
        assert out.confidence_score == 0.0


class TestBaseHelpers:
    def test_clip_clamps_high(self):
        assert _clip(1.5) == 1.0

    def test_clip_clamps_low(self):
        assert _clip(-0.5) == 0.0

    def test_clip_passthrough(self):
        assert _clip(0.5) == pytest.approx(0.5)

    def test_votes_to_probability_zero(self):
        assert _votes_to_probability(0.0, 1.0) == pytest.approx(0.5)

    def test_votes_to_probability_max(self):
        p = _votes_to_probability(1.0, 1.0)
        assert 0.5 < p <= 0.95

    def test_votes_to_probability_zero_max_votes(self):
        assert _votes_to_probability(0.5, 0.0) == pytest.approx(0.5)

    def test_signal_to_output_long(self):
        out = _signal_to_output("test", 5.0, 5.0, 0.8, 0.2)
        assert out.directional_hypothesis == "long"

    def test_signal_to_output_short(self):
        out = _signal_to_output("test", -5.0, 5.0, 0.8, 0.2)
        assert out.directional_hypothesis == "short"

    def test_signal_to_output_neutral_near_zero(self):
        out = _signal_to_output("test", 0.001, 5.0, 0.8, 0.2)
        assert out.directional_hypothesis == "neutral"


# ── Regime Gate ───────────────────────────────────────────────────────────────


class TestRegimeGate:
    def test_trending_filters_momentum(self):
        agents = build_agent_registry()
        compatible = filter_compatible_agents(agents, Regime.TRENDING)
        names = [a.name for a in compatible]
        assert AgentName.MOMENTUM in names

    def test_trending_excludes_mean_reversion(self):
        agents = build_agent_registry()
        compatible = filter_compatible_agents(agents, Regime.TRENDING)
        names = [a.name for a in compatible]
        assert AgentName.MEAN_REVERSION not in names

    def test_range_bound_includes_mean_reversion(self):
        agents = build_agent_registry()
        compatible = filter_compatible_agents(agents, Regime.RANGE_BOUND)
        names = [a.name for a in compatible]
        assert AgentName.MEAN_REVERSION in names

    def test_range_bound_excludes_momentum(self):
        agents = build_agent_registry()
        compatible = filter_compatible_agents(agents, Regime.RANGE_BOUND)
        names = [a.name for a in compatible]
        assert AgentName.MOMENTUM not in names

    def test_none_regime_includes_all_compatible_all_agents(self):
        # Agents with empty compatible_regimes are compatible with all regimes
        agents = [CarryYieldAgent(), LiquidityAgent()]
        compatible = filter_compatible_agents(agents, None)
        assert len(compatible) == 2

    def test_unknown_regime_excludes_restricted_agents(self):
        agents = [MomentumAgent()]  # TRENDING only
        compatible = filter_compatible_agents(agents, "unknown_regime")
        assert len(compatible) == 0


# ── Mean Reversion Agent ──────────────────────────────────────────────────────


class TestMeanReversionAgent:
    @pytest.fixture
    def agent(self):
        return MeanReversionAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.MEAN_REVERSION

    def test_compatible_regimes(self, agent):
        assert Regime.RANGE_BOUND in agent.compatible_regimes
        assert Regime.LOW_VOLATILITY in agent.compatible_regimes
        assert Regime.TRENDING not in agent.compatible_regimes

    async def test_no_zscore_returns_neutral(self, agent):
        out = await agent.analyze(_features(zscore=None))
        assert out.directional_hypothesis == "neutral"

    async def test_z_below_trigger_returns_neutral(self, agent):
        out = await agent.analyze(_features(zscore=2.0))
        assert out.directional_hypothesis == "neutral"

    async def test_positive_z_above_trigger_returns_short(self, agent):
        out = await agent.analyze(_features(zscore=4.0))
        assert out.directional_hypothesis == "short"

    async def test_negative_z_above_trigger_returns_long(self, agent):
        out = await agent.analyze(_features(zscore=-4.0))
        assert out.directional_hypothesis == "long"

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(_features(zscore=5.0))
        _assert_valid_output(out)

    async def test_higher_z_higher_probability(self, agent):
        out_low = await agent.analyze(_features(zscore=4.0))
        out_high = await agent.analyze(_features(zscore=8.0))
        assert out_high.probability_estimate > out_low.probability_estimate


# ── Momentum Agent ────────────────────────────────────────────────────────────


class TestMomentumAgent:
    @pytest.fixture
    def agent(self):
        return MomentumAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.MOMENTUM

    def test_compatible_only_trending(self, agent):
        assert agent.compatible_regimes == (Regime.TRENDING,)

    async def test_no_features_returns_neutral(self, agent):
        out = await agent.analyze(_features())
        assert out.directional_hypothesis == "neutral"

    async def test_bullish_ema_signal_returns_long(self, agent):
        out = await agent.analyze(
            _features(
                ema_signal=500.0,
                ema_slow=50000.0,
                roc_short=0.02,
                roc_long=0.01,
            )
        )
        assert out.directional_hypothesis == "long"

    async def test_bearish_ema_signal_returns_short(self, agent):
        out = await agent.analyze(
            _features(
                ema_signal=-500.0,
                ema_slow=50000.0,
                roc_short=-0.02,
                roc_long=-0.01,
            )
        )
        assert out.directional_hypothesis == "short"

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(_features(ema_signal=200.0, ema_slow=50000.0))
        _assert_valid_output(out)


# ── Technical Structure Agent ─────────────────────────────────────────────────


class TestTechnicalStructureAgent:
    @pytest.fixture
    def agent(self):
        return TechnicalStructureAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.TECHNICAL_STRUCTURE

    async def test_bullish_ema_alignment_long(self, agent):
        # ema_fast > ema_slow → bullish alignment
        out = await agent.analyze(
            _features(
                ema_fast=51000.0,
                ema_slow=50000.0,
                ema_signal=1000.0,
            )
        )
        assert out.directional_hypothesis == "long"

    async def test_bearish_ema_alignment_short(self, agent):
        out = await agent.analyze(
            _features(
                ema_fast=49000.0,
                ema_slow=50000.0,
                ema_signal=-1000.0,
            )
        )
        assert out.directional_hypothesis == "short"

    async def test_no_features_neutral(self, agent):
        out = await agent.analyze(_features())
        assert out.directional_hypothesis == "neutral"

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(_features(ema_fast=51000.0, ema_slow=50000.0))
        _assert_valid_output(out)


# ── Liquidity Agent ───────────────────────────────────────────────────────────


class TestLiquidityAgent:
    @pytest.fixture
    def agent(self):
        return LiquidityAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.LIQUIDITY

    def test_compatible_with_all_regimes(self, agent):
        assert agent.is_compatible_with_regime(Regime.TRENDING)
        assert agent.is_compatible_with_regime(Regime.RANGE_BOUND)
        assert agent.is_compatible_with_regime(Regime.HIGH_VOLATILITY)
        assert agent.is_compatible_with_regime(Regime.LOW_VOLATILITY)
        assert agent.is_compatible_with_regime(None)

    async def test_no_features_neutral(self, agent):
        out = await agent.analyze(_features())
        assert out.directional_hypothesis == "neutral"

    async def test_bid_heavy_imbalance_long(self, agent):
        out = await agent.analyze(_features(book_imbalance=0.6, bid_ask_spread=0.001))
        assert out.directional_hypothesis == "long"

    async def test_ask_heavy_imbalance_short(self, agent):
        out = await agent.analyze(_features(book_imbalance=-0.6, bid_ask_spread=0.001))
        assert out.directional_hypothesis == "short"

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(_features(book_imbalance=0.5, bid_ask_spread=0.002))
        _assert_valid_output(out)


# ── Order Flow Toxicity Agent ─────────────────────────────────────────────────


class TestOrderFlowToxicityAgent:
    @pytest.fixture
    def agent(self):
        return OrderFlowToxicityAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.ORDER_FLOW_TOXICITY

    async def test_no_features_neutral(self, agent):
        out = await agent.analyze(_features())
        assert out.directional_hypothesis == "neutral"

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(_features(bid_ask_spread=0.002, book_imbalance=0.5))
        _assert_valid_output(out)

    async def test_wide_spread_high_risk(self, agent):
        out_narrow = await agent.analyze(_features(bid_ask_spread=0.001, book_imbalance=0.3))
        out_wide = await agent.analyze(_features(bid_ask_spread=0.05, book_imbalance=0.3))
        assert out_wide.risk_estimate >= out_narrow.risk_estimate


# ── Stop-Run Detection Agent ──────────────────────────────────────────────────


class TestStopRunDetectionAgent:
    @pytest.fixture
    def agent(self):
        return StopRunDetectionAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.STOP_RUN_DETECTION

    def test_compatible_regimes(self, agent):
        assert Regime.HIGH_VOLATILITY in agent.compatible_regimes
        assert Regime.TRENDING in agent.compatible_regimes
        assert Regime.RANGE_BOUND not in agent.compatible_regimes

    async def test_no_zscore_neutral(self, agent):
        out = await agent.analyze(_features(zscore=None))
        assert out.directional_hypothesis == "neutral"

    async def test_below_threshold_neutral(self, agent):
        out = await agent.analyze(_features(zscore=3.5))  # threshold is 4.0
        assert out.directional_hypothesis == "neutral"

    async def test_upward_spike_returns_short(self, agent):
        # Z > 4 (stop run up) → fade it → short
        out = await agent.analyze(_features(zscore=5.0, rolling_std=0.003))
        assert out.directional_hypothesis == "short"

    async def test_downward_spike_returns_long(self, agent):
        out = await agent.analyze(_features(zscore=-5.0, rolling_std=0.003))
        assert out.directional_hypothesis == "long"

    async def test_elevated_vol_boosts_confidence(self, agent):
        out_low_vol = await agent.analyze(_features(zscore=5.0, rolling_std=0.001))
        out_high_vol = await agent.analyze(_features(zscore=5.0, rolling_std=0.005))
        assert out_high_vol.confidence_score > out_low_vol.confidence_score

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(_features(zscore=5.0))
        _assert_valid_output(out)


# ── Carry Yield Agent ─────────────────────────────────────────────────────────


class TestCarryYieldAgent:
    @pytest.fixture
    def agent(self):
        return CarryYieldAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.CARRY_YIELD

    def test_compatible_with_all_regimes(self, agent):
        assert agent.is_compatible_with_regime(Regime.TRENDING)
        assert agent.is_compatible_with_regime(None)

    async def test_no_funding_neutral(self, agent):
        out = await agent.analyze(_features())
        assert out.directional_hypothesis == "neutral"

    async def test_long_heavy_returns_short(self, agent):
        out = await agent.analyze(
            _features(
                funding_rate=0.001,
                annualized_funding=0.1095,
                funding_bias="long_heavy",
            )
        )
        assert out.directional_hypothesis == "short"

    async def test_short_heavy_returns_long(self, agent):
        out = await agent.analyze(
            _features(
                funding_rate=-0.001,
                annualized_funding=-0.1095,
                funding_bias="short_heavy",
            )
        )
        assert out.directional_hypothesis == "long"

    async def test_neutral_bias_returns_neutral(self, agent):
        out = await agent.analyze(
            _features(
                funding_rate=0.00001,
                annualized_funding=0.001,
                funding_bias="neutral",
            )
        )
        assert out.directional_hypothesis == "neutral"

    async def test_high_funding_higher_probability(self, agent):
        out_low = await agent.analyze(
            _features(
                funding_rate=0.0001,
                annualized_funding=0.01,
                funding_bias="long_heavy",
            )
        )
        out_high = await agent.analyze(
            _features(
                funding_rate=0.003,
                annualized_funding=0.33,
                funding_bias="long_heavy",
            )
        )
        assert out_high.probability_estimate > out_low.probability_estimate

    async def test_output_valid_rule3(self, agent):
        out = await agent.analyze(
            _features(
                funding_rate=0.001,
                annualized_funding=0.1,
                funding_bias="long_heavy",
            )
        )
        _assert_valid_output(out)


# ── Macro Sentiment Agent ─────────────────────────────────────────────────────


class TestMacroSentimentAgent:
    @pytest.fixture
    def agent(self):
        return MacroSentimentAgent()

    def test_name(self, agent):
        assert agent.name == AgentName.MACRO_SENTIMENT

    def test_compatible_with_all_regimes(self, agent):
        assert agent.is_compatible_with_regime(Regime.TRENDING)
        assert agent.is_compatible_with_regime(None)

    async def test_always_returns_valid_output(self, agent):
        out = await agent.analyze(_features())
        _assert_valid_output(out)

    async def test_trending_regime_non_neutral(self, agent):
        # In trending regime macro should produce a directional view
        out = await agent.analyze(
            _features(
                regime=Regime.TRENDING,
                ema_signal=500.0,
                ema_slow=50000.0,
            )
        )
        _assert_valid_output(out)

    async def test_low_confidence_phase1(self, agent):
        # Phase 1 proxy logic — confidence should be modest
        out = await agent.analyze(_features())
        assert out.confidence_score <= 0.5


# ── Agent Runner ──────────────────────────────────────────────────────────────


class TestAgentRunner:
    def test_build_agent_registry_has_8_agents(self):
        agents = build_agent_registry()
        assert len(agents) == 8

    def test_build_agent_registry_all_unique_names(self):
        agents = build_agent_registry()
        names = [a.name for a in agents]
        assert len(names) == len(set(names))

    def test_build_agent_registry_all_8_names_present(self):
        agents = build_agent_registry()
        names = {a.name for a in agents}
        expected = {
            AgentName.MOMENTUM,
            AgentName.TECHNICAL_STRUCTURE,
            AgentName.LIQUIDITY,
            AgentName.ORDER_FLOW_TOXICITY,
            AgentName.STOP_RUN_DETECTION,
            AgentName.MEAN_REVERSION,
            AgentName.CARRY_YIELD,
            AgentName.MACRO_SENTIMENT,
        }
        assert names == expected

    async def test_run_agents_returns_list(self):
        features = _features(regime=Regime.TRENDING)
        results = await run_agents(features)
        assert isinstance(results, list)

    async def test_run_agents_all_outputs_valid(self):
        features = _features(regime=Regime.TRENDING)
        results = await run_agents(features)
        for out in results:
            _assert_valid_output(out)

    async def test_run_agents_trending_excludes_mean_reversion(self):
        features = _features(regime=Regime.TRENDING)
        results = await run_agents(features)
        names = agent_names_ran(results)
        assert AgentName.MEAN_REVERSION not in names

    async def test_run_agents_range_bound_excludes_momentum(self):
        features = _features(regime=Regime.RANGE_BOUND)
        results = await run_agents(features)
        names = agent_names_ran(results)
        assert AgentName.MOMENTUM not in names

    async def test_run_agents_accepts_custom_agent_list(self):
        features = _features(regime=Regime.TRENDING)
        agents = [CarryYieldAgent(), LiquidityAgent()]
        results = await run_agents(features, agents=agents)
        assert len(results) == 2

    def test_agent_names_ran_extracts_names(self):
        outputs = [
            AgentOutput(AgentName.MOMENTUM, "long", 0.7, 0.6, 0.3),
            AgentOutput(AgentName.LIQUIDITY, "neutral", 0.5, 0.0, 0.5),
        ]
        assert agent_names_ran(outputs) == [AgentName.MOMENTUM, AgentName.LIQUIDITY]
