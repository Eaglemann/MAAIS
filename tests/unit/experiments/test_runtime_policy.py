from dataclasses import replace
from decimal import Decimal

import pytest

from maais.config.modes import RunMode
from maais.experiments.runtime_policy import LivePaperPolicy, RuntimePolicyError
from tests.unit.experiments.test_manifest import _manifest


def _live_manifest(**overrides):
    values = {
        "mode": RunMode.PAPER_LIVE,
        "configuration": {
            "risk": {"leverage": 1},
            "runtime": {"proposal_ttl_seconds": "30", "history_bars": 240},
            "benchmark": {
                "symbol": "BTCUSDT",
                "horizon_bars": 60,
                "source": "binance_spot_close",
            },
        },
        "component_versions": {
            "features": "v1",
            "integrity": "v1",
            "decision": "v1",
            "monitoring": "v1",
            "risk": "v1",
            "exit": "v1",
            "fill": "v1",
            "protection": "v1",
            "counterfactual": "v1",
        },
        "clock_policy": {"latency_ms": 250, "maximum_decision_lag_ms": 5000},
        "market_data_sources": {
            "futures": "binance_usdm",
            "primary_spot": "binance_spot",
            "secondary_venue": "bybit_spot",
        },
    }
    values.update(overrides)
    return _manifest(**values)


def test_live_policy_extracts_every_execution_critical_value_without_defaults() -> None:
    policy = LivePaperPolicy.from_manifest(_live_manifest())

    assert policy.leverage == 1
    assert policy.proposal_ttl.total_seconds() == 30
    assert policy.execution_latency.total_seconds() == 0.25
    assert policy.maximum_decision_lag.total_seconds() == 5
    assert policy.maker_fee_rate == Decimal("0.0002")
    assert policy.taker_fee_rate == Decimal("0.0004")
    assert policy.history_bars == 240
    assert policy.benchmark_symbol == "BTCUSDT"
    assert policy.integrity_policy().max_decision_lag == policy.maximum_decision_lag


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"mode": RunMode.REPLAY}, "mode=paper_live"),
        ({"configuration": {"risk": {"leverage": 1}}}, "runtime"),
        (
            {
                "configuration": {
                    "risk": {"leverage": 2},
                    "runtime": {"proposal_ttl_seconds": "30", "history_bars": 240},
                    "benchmark": {
                        "symbol": "BTCUSDT",
                        "horizon_bars": 60,
                        "source": "binance_spot_close",
                    },
                }
            },
            "leverage exactly 1",
        ),
        ({"clock_policy": {"latency_ms": 250}}, "maximum_decision_lag_ms"),
        ({"fee_policy": {"maker": "0.001", "taker": "0.0004"}}, "fees"),
        ({"funding_policy": {"source": "estimated"}}, "funding source"),
        (
            {
                "market_data_sources": {
                    "futures": "binance_usdm",
                    "primary_spot": "binance_spot",
                    "secondary_venue": "coinbase",
                }
            },
            "secondary_venue",
        ),
    ),
)
def test_live_policy_fails_closed_on_missing_or_unsafe_values(changes, message) -> None:
    manifest = _live_manifest()
    manifest = replace(manifest, **changes)

    with pytest.raises(RuntimePolicyError, match=message):
        LivePaperPolicy.from_manifest(manifest)


def test_live_policy_requires_every_authoritative_component_version() -> None:
    manifest = _live_manifest()
    manifest = replace(
        manifest,
        component_versions={
            name: version
            for name, version in manifest.component_versions.items()
            if name != "counterfactual"
        },
    )

    with pytest.raises(RuntimePolicyError, match="counterfactual"):
        LivePaperPolicy.from_manifest(manifest)
