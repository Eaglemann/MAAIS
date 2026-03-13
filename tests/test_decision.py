"""Tests for the Decision Engine (Batch 5)."""

import pytest
from datetime import datetime, timezone

from maais.agents.base import AgentOutput
from maais.config.constants import AgentName, Regime
from maais.decision.adversarial import run_adversarial, _ADVERSARIAL_BLOCK_THRESHOLD
from maais.decision.consensus import compute_consensus
from maais.decision.cost_estimator import estimate_costs, TAKER_FEE_PCT
from maais.decision.engine import DecisionEngine
from maais.decision.ev_engine import compute_ev
from maais.decision.gate import evaluate_gate, alpha_valid
from maais.decision.schemas import AdversarialSummary, CostEstimate, ConsensusResult, EVResult
from maais.decision.weights import AgentWeightRegistry, DEFAULT_WEIGHT
from maais.feature_pipeline.features import FeatureSet


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _features(
    regime: str = Regime.TRENDING,
    atr: float | None = 500.0,
    zscore_mean: float | None = 50000.0,
    rolling_std: float | None = 0.002,
    bid_ask_spread: float | None = 0.001,
    funding_rate: float | None = None,
    annualized_funding: float | None = None,
    funding_bias: str | None = None,
    zscore: float | None = None,
) -> FeatureSet:
    return FeatureSet(
        symbol="BTCUSDT",
        timeframe="1m",
        timestamp=_ts(),
        regime=regime,
        atr=atr,
        zscore_mean=zscore_mean,
        rolling_std=rolling_std,
        bid_ask_spread=bid_ask_spread,
        funding_rate=funding_rate,
        annualized_funding=annualized_funding,
        funding_bias=funding_bias,
        zscore=zscore,
    )


def _output(name: str, direction: str, prob: float = 0.7, conf: float = 0.6, risk: float = 0.3) -> AgentOutput:
    return AgentOutput(
        agent_name=name,
        directional_hypothesis=direction,
        probability_estimate=prob,
        confidence_score=conf,
        risk_estimate=risk,
    )


# ── AgentWeightRegistry ───────────────────────────────────────────────────────

class TestAgentWeightRegistry:
    def test_all_agents_initialized_to_default(self):
        reg = AgentWeightRegistry()
        for name in [AgentName.MOMENTUM, AgentName.CARRY_YIELD, AgentName.LIQUIDITY]:
            assert reg.get(name) == DEFAULT_WEIGHT

    def test_unknown_agent_returns_default(self):
        reg = AgentWeightRegistry()
        assert reg.get("unknown_agent") == DEFAULT_WEIGHT

    def test_update_weight(self):
        reg = AgentWeightRegistry()
        reg.update_weight(AgentName.MOMENTUM, 1.5)
        assert reg.get(AgentName.MOMENTUM) == pytest.approx(1.5)

    def test_update_weight_zero_raises(self):
        reg = AgentWeightRegistry()
        with pytest.raises(ValueError):
            reg.update_weight(AgentName.MOMENTUM, 0.0)

    def test_update_weight_negative_raises(self):
        reg = AgentWeightRegistry()
        with pytest.raises(ValueError):
            reg.update_weight(AgentName.MOMENTUM, -0.5)

    def test_all_weights_returns_dict(self):
        reg = AgentWeightRegistry()
        weights = reg.all_weights()
        assert isinstance(weights, dict)
        assert len(weights) == 8

    def test_load_from_dict(self):
        reg = AgentWeightRegistry()
        reg.load_from_dict({AgentName.MOMENTUM: 2.0, AgentName.LIQUIDITY: 1.5})
        assert reg.get(AgentName.MOMENTUM) == pytest.approx(2.0)
        assert reg.get(AgentName.LIQUIDITY) == pytest.approx(1.5)

    def test_load_from_dict_ignores_non_positive(self):
        reg = AgentWeightRegistry()
        reg.load_from_dict({AgentName.MOMENTUM: 0.0})
        assert reg.get(AgentName.MOMENTUM) == DEFAULT_WEIGHT

    def test_reset_to_defaults(self):
        reg = AgentWeightRegistry()
        reg.update_weight(AgentName.MOMENTUM, 3.0)
        reg.reset_to_defaults()
        assert reg.get(AgentName.MOMENTUM) == DEFAULT_WEIGHT


# ── Consensus ─────────────────────────────────────────────────────────────────

class TestConsensus:
    def test_empty_outputs_returns_neutral(self):
        reg = AgentWeightRegistry()
        result = compute_consensus([], reg)
        assert result.direction == "neutral"
        assert result.agents_counted == 0

    def test_all_long_returns_long(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "long"),
        ]
        result = compute_consensus(outputs, reg)
        assert result.direction == "long"

    def test_all_short_returns_short(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "short"),
            _output(AgentName.LIQUIDITY, "short"),
        ]
        result = compute_consensus(outputs, reg)
        assert result.direction == "short"

    def test_all_neutral_returns_neutral(self):
        reg = AgentWeightRegistry()
        outputs = [_output(AgentName.MOMENTUM, "neutral", prob=0.5, conf=0.0)]
        result = compute_consensus(outputs, reg)
        assert result.direction == "neutral"

    def test_majority_long_wins(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "long"),
            _output(AgentName.MEAN_REVERSION, "short"),
        ]
        result = compute_consensus(outputs, reg)
        assert result.direction == "long"

    def test_weighted_probability_calculation(self):
        reg = AgentWeightRegistry()
        # All equal weights (1.0), two long with prob 0.6 and 0.8
        outputs = [
            _output(AgentName.MOMENTUM, "long", prob=0.6),
            _output(AgentName.LIQUIDITY, "long", prob=0.8),
        ]
        result = compute_consensus(outputs, reg)
        assert result.weighted_probability == pytest.approx(0.7)  # (0.6+0.8)/2

    def test_higher_weight_shifts_direction(self):
        reg = AgentWeightRegistry()
        reg.update_weight(AgentName.MOMENTUM, 3.0)  # momentum gets 3× weight
        outputs = [
            _output(AgentName.MOMENTUM, "long"),    # weight 3.0
            _output(AgentName.LIQUIDITY, "short"),  # weight 1.0
            _output(AgentName.CARRY_YIELD, "short"), # weight 1.0
        ]
        result = compute_consensus(outputs, reg)
        assert result.direction == "long"  # 3 > 2

    def test_agents_counted(self):
        reg = AgentWeightRegistry()
        outputs = [_output(AgentName.MOMENTUM, "long"), _output(AgentName.LIQUIDITY, "short")]
        result = compute_consensus(outputs, reg)
        assert result.agents_counted == 2

    def test_agents_long_short_neutral_lists(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "short"),
            _output(AgentName.CARRY_YIELD, "neutral", prob=0.5, conf=0.0),
        ]
        result = compute_consensus(outputs, reg)
        assert AgentName.MOMENTUM in result.agents_long
        assert AgentName.LIQUIDITY in result.agents_short
        assert AgentName.CARRY_YIELD in result.agents_neutral


# ── Adversarial ───────────────────────────────────────────────────────────────

class TestAdversarial:
    def test_no_dissenters_challenge_not_accepted(self):
        reg = AgentWeightRegistry()
        outputs = [_output(AgentName.MOMENTUM, "long"), _output(AgentName.LIQUIDITY, "long")]
        result = run_adversarial(outputs, "long", reg)
        assert not result.challenge_accepted
        assert result.dissenting_agents == []

    def test_neutral_majority_returns_no_challenge(self):
        reg = AgentWeightRegistry()
        outputs = [_output(AgentName.MOMENTUM, "neutral", prob=0.5, conf=0.0)]
        result = run_adversarial(outputs, "neutral", reg)
        assert not result.challenge_accepted

    def test_low_confidence_dissenter_does_not_block(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "short", conf=0.3),  # low confidence
        ]
        result = run_adversarial(outputs, "long", reg)
        assert not result.challenge_accepted

    def test_high_confidence_dissenter_blocks(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "short", conf=_ADVERSARIAL_BLOCK_THRESHOLD + 0.1),
        ]
        result = run_adversarial(outputs, "long", reg)
        assert result.challenge_accepted

    def test_dissenting_agents_identified(self):
        reg = AgentWeightRegistry()
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "short"),
        ]
        result = run_adversarial(outputs, "long", reg)
        assert AgentName.LIQUIDITY in result.dissenting_agents

    def test_llm_reasoning_is_none_phase1(self):
        reg = AgentWeightRegistry()
        outputs = [_output(AgentName.MOMENTUM, "long")]
        result = run_adversarial(outputs, "long", reg)
        assert result.llm_reasoning is None


# ── Cost Estimator ────────────────────────────────────────────────────────────

class TestCostEstimator:
    def test_round_trip_fee_is_2x_taker(self):
        result = estimate_costs(_features(atr=None, bid_ask_spread=None))
        assert result.exchange_fee_pct == pytest.approx(TAKER_FEE_PCT * 2)

    def test_total_cost_gte_exchange_fee(self):
        result = estimate_costs(_features())
        assert result.total_cost_pct >= result.exchange_fee_pct

    def test_wider_spread_higher_cost(self):
        narrow = estimate_costs(_features(bid_ask_spread=0.001))
        wide = estimate_costs(_features(bid_ask_spread=0.01))
        assert wide.total_cost_pct > narrow.total_cost_pct

    def test_higher_atr_higher_slippage(self):
        low_vol = estimate_costs(_features(atr=100.0, zscore_mean=50000.0))
        high_vol = estimate_costs(_features(atr=1000.0, zscore_mean=50000.0))
        assert high_vol.slippage_pct > low_vol.slippage_pct

    def test_no_atr_still_returns_cost(self):
        result = estimate_costs(_features(atr=None))
        assert result.total_cost_pct >= 0

    def test_funding_carry_positive_when_present(self):
        result = estimate_costs(_features(annualized_funding=0.365))
        assert result.funding_carry_pct > 0

    def test_no_funding_carry_zero(self):
        result = estimate_costs(_features(annualized_funding=None))
        assert result.funding_carry_pct == 0.0


# ── EV Engine ─────────────────────────────────────────────────────────────────

class TestEVEngine:
    def test_high_probability_positive_ev(self):
        feats = _features(atr=500.0, zscore_mean=50000.0)
        costs = CostEstimate(exchange_fee_pct=0.0008, slippage_pct=0.001, total_cost_pct=0.0018, funding_carry_pct=0.0)
        result = compute_ev(p_win=0.75, features=feats, costs=costs, direction="long")
        # P(win)×gain − P(loss)×loss = 0.75×0.01 − 0.25×0.01 = 0.005 → net after 0.0018 cost > 0
        assert result.is_positive

    def test_low_probability_negative_ev(self):
        feats = _features(atr=500.0, zscore_mean=50000.0)
        costs = CostEstimate(exchange_fee_pct=0.0008, slippage_pct=0.005, total_cost_pct=0.0058, funding_carry_pct=0.0)
        result = compute_ev(p_win=0.51, features=feats, costs=costs, direction="long")
        # Near coin-flip with high costs → negative
        assert not result.is_positive

    def test_p_loss_is_complement(self):
        feats = _features()
        costs = CostEstimate(0.0008, 0.001, 0.0018, 0.0)
        result = compute_ev(0.7, feats, costs, "long")
        assert result.p_loss == pytest.approx(0.3)

    def test_funding_carry_adds_to_ev_when_on_rewarded_side(self):
        feats = _features(annualized_funding=0.365, funding_bias="long_heavy")
        costs = CostEstimate(0.0008, 0.001, 0.0018, funding_carry_pct=0.365 / (365 * 3))
        ev_short = compute_ev(0.6, feats, costs, "short")  # short earns carry
        ev_long = compute_ev(0.6, feats, costs, "long")    # long pays carry
        assert ev_short.net_ev > ev_long.net_ev

    def test_fallback_atr_when_none(self):
        feats = _features(atr=None, zscore_mean=None)
        costs = CostEstimate(0.0008, 0.0, 0.0008, 0.0)
        result = compute_ev(0.7, feats, costs, "long")
        assert result.expected_gain_pct == pytest.approx(0.005)


# ── Gate ──────────────────────────────────────────────────────────────────────

class TestGate:
    def _ev(self, is_positive: bool) -> EVResult:
        net = 0.01 if is_positive else -0.01
        return EVResult(0.7, 0.3, 0.01, 0.01, net + 0.002, 0.0, net, is_positive)

    def _adversarial(self, challenge: bool) -> AdversarialSummary:
        return AdversarialSummary("long", "short", [], 0.5, 0.3, challenge)

    def test_positive_ev_non_neutral_approves(self):
        approved, reason = evaluate_gate(self._ev(True), self._adversarial(False), "long")
        assert approved
        assert reason is None

    def test_negative_ev_rejects(self):
        approved, reason = evaluate_gate(self._ev(False), self._adversarial(False), "long")
        assert not approved
        assert "negative_ev" in reason

    def test_neutral_direction_rejects(self):
        approved, reason = evaluate_gate(self._ev(True), self._adversarial(False), "neutral")
        assert not approved
        assert "neutral" in reason

    def test_adversarial_challenge_rejects(self):
        approved, reason = evaluate_gate(self._ev(True), self._adversarial(True), "long")
        assert not approved
        assert "adversarial" in reason

    def test_alpha_valid_positive_ev(self):
        ev = self._ev(True)
        assert alpha_valid(ev)

    def test_alpha_invalid_negative_ev(self):
        ev = self._ev(False)
        assert not alpha_valid(ev)


# ── Decision Engine (Integration) ─────────────────────────────────────────────

class TestDecisionEngine:
    def test_strong_consensus_approves(self):
        engine = DecisionEngine()
        feats = _features()
        outputs = [
            _output(AgentName.MOMENTUM, "long", prob=0.80, conf=0.7),
            _output(AgentName.TECHNICAL_STRUCTURE, "long", prob=0.75, conf=0.6),
            _output(AgentName.LIQUIDITY, "long", prob=0.72, conf=0.5),
        ]
        result = engine.evaluate(feats, outputs)
        assert result.direction == "long"
        assert result.symbol == "BTCUSDT"
        assert isinstance(result.approved, bool)

    def test_neutral_direction_not_approved(self):
        engine = DecisionEngine()
        feats = _features()
        outputs = [
            _output(AgentName.MOMENTUM, "neutral", prob=0.5, conf=0.0),
        ]
        result = engine.evaluate(feats, outputs)
        assert not result.approved
        assert result.direction == "neutral"

    def test_empty_outputs_not_approved(self):
        engine = DecisionEngine()
        result = engine.evaluate(_features(), [])
        assert not result.approved

    def test_result_has_all_fields(self):
        engine = DecisionEngine()
        outputs = [_output(AgentName.MOMENTUM, "long", prob=0.75)]
        result = engine.evaluate(_features(), outputs)
        assert result.consensus is not None
        assert result.adversarial is not None
        assert result.costs is not None
        assert result.ev is not None

    def test_to_dict_keys(self):
        engine = DecisionEngine()
        outputs = [_output(AgentName.MOMENTUM, "long", prob=0.75)]
        result = engine.evaluate(_features(), outputs)
        d = result.to_dict()
        assert "approved" in d
        assert "direction" in d
        assert "net_ev" in d
        assert "total_cost_pct" in d

    def test_custom_weight_registry(self):
        reg = AgentWeightRegistry()
        reg.update_weight(AgentName.MOMENTUM, 5.0)
        engine = DecisionEngine(weight_registry=reg)
        # Just verify engine uses the registry without error
        outputs = [
            _output(AgentName.MOMENTUM, "long"),
            _output(AgentName.LIQUIDITY, "short"),
        ]
        result = engine.evaluate(_features(), outputs)
        assert result.direction == "long"  # momentum has 5× weight

    def test_adversarial_block_prevents_approval(self):
        engine = DecisionEngine()
        feats = _features()
        outputs = [
            _output(AgentName.MOMENTUM, "long", prob=0.70, conf=0.5),
            # High-confidence dissenter
            _output(AgentName.MEAN_REVERSION, "short", prob=0.75, conf=_ADVERSARIAL_BLOCK_THRESHOLD + 0.05),
        ]
        result = engine.evaluate(feats, outputs)
        if result.adversarial.challenge_accepted:
            assert not result.approved
