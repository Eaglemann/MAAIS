from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from maais.agents.evaluations import (
    AgentEvaluation,
    AgentEvaluationMatrix,
    AgentOutputSnapshot,
)
from maais.config.constants import ALL_AGENTS, AgentName, Regime
from maais.decision.official import OfficialDecisionAnalytics, OfficialDecisionPolicy
from maais.domain.enums import AgentMaturity, Direction
from maais.feature_pipeline.features import FeatureSet
from tests.unit.experiments.test_manifest import _manifest

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _matrix(directions: tuple[Direction, ...]) -> AgentEvaluationMatrix:
    manifest = _manifest()
    rows = tuple(
        AgentEvaluation(
            agent_name=name,
            version=manifest.agent_versions[index].version,
            maturity=(
                AgentMaturity.PROXY
                if name == AgentName.MACRO_SENTIMENT
                else AgentMaturity.IMPLEMENTED
            ),
            proxy_label=("technical_features_proxy" if name == AgentName.MACRO_SENTIMENT else None),
            weight=Decimal("1"),
            enabled=True,
            compatible=True,
            voting=True,
            reason_codes=("agent_evaluated",),
            input_contributions={"frame": "test"},
            duration_ms=0,
            output=AgentOutputSnapshot(
                direction=directions[index],
                probability=Decimal("0.70"),
                confidence=Decimal("0.80"),
                risk=Decimal("0.20"),
            ),
        )
        for index, name in enumerate(ALL_AGENTS)
    )
    return AgentEvaluationMatrix(
        manifest.experiment_id,
        "BTCUSDT",
        "1m",
        NOW,
        Regime.TRENDING,
        rows,
    )


def _features(**overrides: object) -> FeatureSet:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "timestamp": NOW,
        "regime": Regime.TRENDING,
        "atr": 1.0,
        "bid_ask_spread": 0.001,
        "annualized_funding": 0.1095,
        "funding_bias": "long_heavy",
    }
    values.update(overrides)
    return FeatureSet(**values)  # type: ignore[arg-type]


def test_conservative_policy_derives_round_trip_cost_from_frozen_taker_fee() -> None:
    policy = OfficialDecisionPolicy.conservative(taker_fee_fraction=Decimal("0.0005"))

    assert policy.round_trip_fee_fraction == Decimal("0.0010")


def test_decimal_analysis_is_directional_costed_and_benchmark_relative() -> None:
    analysis = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
        features=_features(),
        matrix=_matrix((Direction.LONG,) * 8),
        executable_price=Decimal("100"),
        benchmark_return=Decimal("0.001"),
    )

    assert analysis.direction is Direction.LONG
    assert analysis.consensus_probability == Decimal("0.70")
    assert analysis.consensus_confidence == Decimal("0.80")
    assert analysis.long_weight == Decimal("8")
    assert analysis.estimated_cost == Decimal("0.0063")
    assert analysis.funding_carry == Decimal("-0.0001")
    assert analysis.gross_ev == Decimal("0.0040")
    assert analysis.net_ev == Decimal("-0.0024")
    assert analysis.alpha_estimate == Decimal("-0.0034")
    assert not analysis.ev_positive
    assert not analysis.alpha_positive
    assert len(analysis.content_hash) == 64


def test_exact_directional_tie_is_neutral_instead_of_long_biased() -> None:
    directions = (Direction.LONG,) * 4 + (Direction.SHORT,) * 4

    analysis = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
        features=_features(),
        matrix=_matrix(directions),
        executable_price=Decimal("100"),
        benchmark_return=Decimal("0"),
    )

    assert analysis.direction is Direction.NEUTRAL
    assert not analysis.consensus_passed
    assert analysis.consensus_reason == "directional_weight_tie"
    assert analysis.net_ev == Decimal("0")


def test_missing_or_invalid_atr_fails_closed_without_a_fallback_move() -> None:
    for atr in (None, 0.0, -1.0):
        analysis = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
            features=_features(atr=atr),
            matrix=_matrix((Direction.SHORT,) * 8),
            executable_price=Decimal("100"),
            benchmark_return=Decimal("0"),
        )

        assert analysis.direction is Direction.SHORT
        assert not analysis.ev_positive
        assert analysis.ev_reason == "expected_move_unavailable"
        assert analysis.expected_gain == Decimal("0")
        assert analysis.expected_loss == Decimal("0")


def test_missing_spread_and_implausibly_large_atr_each_fail_closed() -> None:
    missing_cost = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
        features=_features(bid_ask_spread=None),
        matrix=_matrix((Direction.LONG,) * 8),
        executable_price=Decimal("100"),
        benchmark_return=Decimal("0"),
    )
    outlier_move = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
        features=_features(atr=25.0),
        matrix=_matrix((Direction.LONG,) * 8),
        executable_price=Decimal("100"),
        benchmark_return=Decimal("0"),
    )

    assert not missing_cost.ev_positive
    assert missing_cost.ev_reason == "cost_inputs_unavailable"
    assert not outlier_move.ev_positive
    assert outlier_move.ev_reason == "expected_move_out_of_bounds"


def test_missing_benchmark_is_explicit_and_cannot_pass_alpha() -> None:
    analysis = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
        features=_features(atr=0.1, bid_ask_spread=0.0001, annualized_funding=None),
        matrix=_matrix((Direction.LONG,) * 8),
        executable_price=Decimal("100"),
        benchmark_return=None,
    )

    assert not analysis.benchmark_available
    assert analysis.benchmark_return == Decimal("0")
    assert not analysis.alpha_positive
    assert analysis.alpha_reason == "benchmark_unavailable"


def test_strong_dissent_blocks_even_when_majority_exists() -> None:
    matrix = _matrix((Direction.LONG,) * 6 + (Direction.SHORT,) * 2)
    dissenters = tuple(
        replace(
            row,
            output=replace(row.output, confidence=Decimal("0.90")),
        )
        if row.output.direction is Direction.SHORT
        else row
        for row in matrix.evaluations
    )

    analysis = OfficialDecisionAnalytics(OfficialDecisionPolicy.conservative()).evaluate(
        features=_features(atr=0.1, bid_ask_spread=0.0001, annualized_funding=None),
        matrix=replace(matrix, evaluations=dissenters),
        executable_price=Decimal("100"),
        benchmark_return=Decimal("0"),
    )

    assert analysis.direction is Direction.LONG
    assert analysis.challenge_blocked
    assert analysis.dissent_confidence == Decimal("0.90")
