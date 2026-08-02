from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from maais.agents.base import AgentOutput, BaseAgent
from maais.agents.evaluations import (
    AgentMatrixError,
    DeterministicDurationSource,
    run_agent_matrix,
)
from maais.agents.runner import build_agent_registry
from maais.config.constants import ALL_AGENTS, AgentName, Regime
from maais.config.modes import RunMode
from maais.domain.enums import AgentMaturity, Direction
from tests.test_agents import _features
from tests.unit.experiments.test_manifest import _manifest


class _ThrowingAgent(BaseAgent):
    name = AgentName.MOMENTUM
    compatible_regimes = (Regime.TRENDING,)

    async def analyze(self, features):
        raise RuntimeError("simulated failure")


class _WrongIdentityAgent(BaseAgent):
    name = AgentName.MOMENTUM
    compatible_regimes = (Regime.TRENDING,)

    async def analyze(self, features):
        return AgentOutput(AgentName.LIQUIDITY, "long", 0.7, 0.8, 0.2)


async def test_official_matrix_has_exactly_eight_ordered_visible_rows() -> None:
    durations = DeterministicDurationSource(
        {name: index for index, name in enumerate(ALL_AGENTS, start=1)}
    )

    matrix = await run_agent_matrix(
        _features(regime=Regime.TRENDING, ema_signal=1.0),
        _manifest(),
        durations=durations,
    )

    assert tuple(row.agent_name for row in matrix.evaluations) == ALL_AGENTS
    assert matrix.experiment_id == _manifest().experiment_id
    assert matrix.symbol == "BTCUSDT"
    assert len(matrix.content_hash) == 64
    assert tuple(row.duration_ms for row in matrix.evaluations) == (1, 2, 3, 4, 5, 0, 7, 8)
    mean_reversion = matrix.by_name(AgentName.MEAN_REVERSION)
    assert not mean_reversion.compatible
    assert not mean_reversion.voting
    assert mean_reversion.output.direction is Direction.NEUTRAL
    assert mean_reversion.reason_codes == ("incompatible_regime",)
    macro = matrix.by_name(AgentName.MACRO_SENTIMENT)
    assert macro.maturity is AgentMaturity.PROXY
    assert macro.proxy_label == "technical_features_proxy"
    assert macro.input_contributions["declared_dependencies"] == {"market_frame": "v1"}


async def test_disabled_agent_is_present_neutral_and_nonvoting() -> None:
    entries = list(_manifest().agent_versions)
    liquidity_index = ALL_AGENTS.index(AgentName.LIQUIDITY)
    entries[liquidity_index] = replace(
        entries[liquidity_index],
        enabled=False,
        maturity=AgentMaturity.DISABLED,
    )

    matrix = await run_agent_matrix(
        _features(regime=Regime.TRENDING),
        _manifest(agent_versions=tuple(entries)),
    )

    liquidity = matrix.by_name(AgentName.LIQUIDITY)
    assert not liquidity.enabled
    assert not liquidity.voting
    assert liquidity.output.direction is Direction.NEUTRAL
    assert liquidity.reason_codes == ("disabled_agent",)
    assert liquidity.duration_ms == 0


async def test_registry_permutation_is_replay_stable_and_live_uses_nonnegative_timing() -> None:
    features = _features(regime=Regime.TRENDING, ema_signal=1.0)
    durations = DeterministicDurationSource({name: 2 for name in ALL_AGENTS})
    first = await run_agent_matrix(
        features,
        _manifest(),
        agents=build_agent_registry(),
        durations=durations,
    )
    second = await run_agent_matrix(
        features,
        _manifest(),
        agents=tuple(reversed(build_agent_registry())),
        durations=durations,
    )
    live = await run_agent_matrix(
        features,
        _manifest(mode=RunMode.PAPER_LIVE),
        agents=build_agent_registry(),
    )

    assert first.content_hash == second.content_hash
    assert all(item.duration_ms >= 0 for item in live.evaluations)


@pytest.mark.parametrize("bad_agent", [_ThrowingAgent(), _WrongIdentityAgent()])
async def test_throwing_or_malformed_mandatory_agent_blocks_with_eight_rows(
    bad_agent: BaseAgent,
) -> None:
    registry = [
        bad_agent if agent.name == AgentName.MOMENTUM else agent for agent in build_agent_registry()
    ]

    with pytest.raises(AgentMatrixError) as caught:
        await run_agent_matrix(
            _features(regime=Regime.TRENDING),
            _manifest(),
            agents=registry,
        )

    assert tuple(row.agent_name for row in caught.value.matrix.evaluations) == ALL_AGENTS
    failed = caught.value.matrix.by_name(AgentName.MOMENTUM)
    assert failed.output.direction is Direction.NEUTRAL
    assert not failed.voting
    assert failed.reason_codes == ("agent_exception",)
    assert caught.value.failures[0].agent_name == AgentName.MOMENTUM


async def test_missing_or_duplicate_registry_agent_blocks_before_analysis() -> None:
    registry = build_agent_registry()
    missing = registry[1:]
    duplicate = [*registry, registry[0]]

    for invalid in (missing, duplicate):
        with pytest.raises(AgentMatrixError) as caught:
            await run_agent_matrix(
                _features(regime=Regime.TRENDING),
                _manifest(),
                agents=invalid,
            )
        assert len(caught.value.matrix.evaluations) == 8
        assert caught.value.failures


def test_agent_output_validation_uses_runtime_exceptions_and_finite_numbers() -> None:
    with pytest.raises(ValueError, match="hypothesis"):
        AgentOutput("test", "up", 0.7, 0.8, 0.2)
    with pytest.raises(ValueError, match="finite"):
        AgentOutput("test", "long", float("nan"), 0.8, 0.2)
    with pytest.raises(ValueError, match="range"):
        AgentOutput("test", "long", 0.7, 1.1, 0.2)
    with pytest.raises(ValueError, match="agent_name"):
        AgentOutput("", "neutral", 0.5, 0.0, 0.5)


def test_agent_matrix_decimal_output_is_bounded() -> None:
    output = AgentOutput(AgentName.MOMENTUM, "long", 0.7, 0.8, 0.2)
    assert Decimal(str(output.probability_estimate)) == Decimal("0.7")
