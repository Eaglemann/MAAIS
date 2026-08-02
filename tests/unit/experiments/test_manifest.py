from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.config.constants import ALL_AGENTS, AgentName
from maais.config.modes import RunMode
from maais.domain.enums import AgentMaturity
from maais.experiments.manifest import (
    AgentManifestEntry,
    ExperimentManifest,
    require_candidate_identity,
)


def _agents() -> tuple[AgentManifestEntry, ...]:
    return tuple(
        AgentManifestEntry(
            agent_name=name,
            version="1.0.0",
            maturity=(
                AgentMaturity.PROXY
                if name == AgentName.MACRO_SENTIMENT
                else AgentMaturity.IMPLEMENTED
            ),
            weight=Decimal("1.0"),
            enabled=True,
            implementation_hash="a" * 64,
            data_dependencies={"market_frame": "v1"},
        )
        for name in ALL_AGENTS
    )


def _manifest(**overrides: object) -> ExperimentManifest:
    values: dict[str, object] = {
        "experiment_id": UUID("11111111-1111-4111-8111-111111111111"),
        "name": "development replay",
        "mode": RunMode.REPLAY,
        "initial_capital": Decimal("10000"),
        "currency": "USDT",
        "created_at": datetime(2026, 8, 2, 10, tzinfo=timezone.utc),
        "git_sha": "1" * 40,
        "worktree_hash": None,
        "lock_hash": "2" * 64,
        "schema_revision": "0006",
        "configuration": {"risk": {"leverage": 1}, "symbols": ["BTCUSDT"]},
        "symbols": ("BTCUSDT",),
        "exchange_metadata": {"venue": "binance", "market": "usdt_perpetual"},
        "component_versions": {
            "features": "v1",
            "decision": "v1",
            "risk": "v1",
            "exit": "v1",
            "fill": "v1",
        },
        "agent_versions": _agents(),
        "fee_policy": {"maker": "0.0002", "taker": "0.0005"},
        "funding_policy": {"source": "observed"},
        "clock_policy": {"latency_ms": 250},
        "market_data_sources": {"primary": "binance", "reference": "coinbase"},
    }
    values.update(overrides)
    return ExperimentManifest(**values)  # type: ignore[arg-type]


def test_manifest_hash_is_stable_and_manifest_is_frozen() -> None:
    first = _manifest(configuration={"b": 2, "a": 1})
    second = _manifest(configuration={"a": 1, "b": 2})

    assert first.config_hash == second.config_hash
    assert first.manifest_hash == second.manifest_hash
    with pytest.raises(FrozenInstanceError):
        first.name = "mutated"  # type: ignore[misc]


def test_candidate_requires_clean_commit() -> None:
    dirty = _manifest(worktree_hash="f" * 64)

    with pytest.raises(ValueError, match="clean committed worktree"):
        require_candidate_identity(dirty)


def test_manifest_requires_all_eight_unique_agents() -> None:
    with pytest.raises(ValueError, match="exactly eight"):
        _manifest(agent_versions=())


def test_macro_agent_must_be_visibly_proxy_or_disabled() -> None:
    agents = list(_agents())
    agents[-1] = replace(agents[-1], maturity=AgentMaturity.IMPLEMENTED)

    with pytest.raises(ValueError, match="macro_sentiment"):
        _manifest(agent_versions=tuple(agents))


def test_nested_configuration_is_detached_and_immutable() -> None:
    configuration = {"risk": {"leverage": 1}}
    manifest = _manifest(configuration=configuration)
    configuration["risk"]["leverage"] = 5

    assert manifest.configuration["risk"]["leverage"] == 1  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.configuration["new"] = "value"  # type: ignore[index]


def test_manifest_round_trip_preserves_identity() -> None:
    manifest = _manifest()

    restored = ExperimentManifest.from_dict(manifest.to_dict())

    assert restored == manifest
    assert restored.manifest_hash == manifest.manifest_hash
