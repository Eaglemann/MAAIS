from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from maais.config.modes import RunMode
from maais.domain.enums import PaperOrderType
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.runtime_policy import LivePaperPolicy, RuntimePolicyError
from tests.unit.experiments.test_manifest import _manifest


def _live_filter(symbol: str = "BTCUSDT") -> ExchangeFilterSnapshot:
    return ExchangeFilterSnapshot(
        symbol=symbol,
        status="TRADING",
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("200"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.LIMIT, PaperOrderType.MARKET),
        captured_at=datetime(2026, 8, 2, 9, tzinfo=timezone.utc),
    )


def _live_manifest(**overrides):
    exchange_filter = _live_filter()
    values = {
        "mode": RunMode.PAPER_LIVE,
        "configuration": {
            "risk": {"leverage": 1},
            "runtime": {
                "proposal_ttl_seconds": "30",
                "book_wait_timeout_seconds": "5",
                "history_bars": 240,
            },
            "benchmark": {
                "symbol": "BTCUSDT",
                "horizon_bars": 60,
                "source": "binance_spot_close",
            },
            "strategy": {
                "key": "maais_primary",
                "version": "1.0.0",
                "stage": "simulation",
                "implementation_hash": "b" * 64,
                "parameters": {"timeframe": "1m"},
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
        "fee_policy": {
            "maker": "0.0002",
            "taker": "0.0005",
            "venue": "binance_usdm",
            "tier": "regular_user",
            "settlement_asset": "USDT",
            "discount": "none",
            "source": "https://www.binance.com/en/fee/trading",
            "verified_at": "2026-08-02T09:00:00Z",
        },
        "exchange_metadata": {
            "venue": "binance_usdm",
            "market": "usdt_perpetual",
            "filter_snapshot_hashes": {"BTCUSDT": exchange_filter.content_hash},
            "filter_snapshots": {"BTCUSDT": exchange_filter.to_dict()},
        },
        "market_data_sources": {
            "futures": "binance_usdm",
            "primary_spot": "binance_spot",
            "secondary_venue": "bybit_spot",
        },
        "data_versions": {
            "input_kind": "public_live_observations",
            "decision_timeframe": "1m",
            "market_frame_schema": "v1",
            "event_ledger_schema": "v1",
            "futures_contract": "binance_usdm_public_v1",
            "primary_spot_contract": "binance_spot_public_v1",
            "secondary_spot_contract": "bybit_spot_public_v1",
            "golden_replay_sha256": (
                "4d2dec967a8fd98ba04616b834c1b247442af3b168409ba6d45bc24833e6b5cc"
            ),
        },
        "fill_policy": {
            "broker": "local_paper",
            "entry_order_type": "market",
            "exit_order_type": "market",
            "book_selection": "first_observed_strictly_after_eligibility",
            "depth_model": "visible_depth_walk_full_or_reject",
            "partial_fill_policy": "market_full_or_reject",
            "liquidity_role": "taker",
            "stale_book_policy": "reject",
            "insufficient_depth_policy": "reject",
            "slippage_model": "spread_plus_depth_plus_latency",
        },
    }
    values.update(overrides)
    return _manifest(**values)


def test_live_policy_extracts_every_execution_critical_value_without_defaults() -> None:
    policy = LivePaperPolicy.from_manifest(_live_manifest())

    assert policy.leverage == 1
    assert policy.proposal_ttl.total_seconds() == 30
    assert policy.book_wait_timeout.total_seconds() == 5
    assert policy.execution_latency.total_seconds() == 0.25
    assert policy.maximum_decision_lag.total_seconds() == 5
    assert policy.maker_fee_rate == Decimal("0.0002")
    assert policy.taker_fee_rate == Decimal("0.0005")
    assert policy.fee_tier == "regular_user"
    assert policy.fee_schedule_verified_at == datetime(2026, 8, 2, 9, tzinfo=timezone.utc)
    assert policy.history_bars == 240
    assert policy.benchmark_symbol == "BTCUSDT"
    assert policy.strategy_key == "maais_primary"
    assert policy.strategy_version == "1.0.0"
    assert policy.strategy_implementation_hash == "b" * 64
    assert policy.strategy_parameters == {"timeframe": "1m"}
    assert policy.exchange_filter_hashes == {"BTCUSDT": _live_filter().content_hash}
    assert policy.exchange_filters == {"BTCUSDT": _live_filter()}
    assert policy.integrity_policy().max_decision_lag == policy.maximum_decision_lag


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"mode": RunMode.REPLAY}, "mode=paper_live"),
        ({"configuration": {"risk": {"leverage": 1}}}, "runtime"),
        (
            {
                "configuration": {
                    "risk": {"leverage": 1},
                    "runtime": {
                        "proposal_ttl_seconds": "30",
                        "book_wait_timeout_seconds": "5",
                        "history_bars": 240,
                    },
                    "benchmark": {
                        "symbol": "BTCUSDT",
                        "horizon_bars": 60,
                        "source": "binance_spot_close",
                    },
                }
            },
            "strategy",
        ),
        (
            {
                "configuration": {
                    "risk": {"leverage": 2},
                    "runtime": {
                        "proposal_ttl_seconds": "30",
                        "book_wait_timeout_seconds": "5",
                        "history_bars": 240,
                    },
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
        (
            {
                "fee_policy": {
                    "maker": "0.001",
                    "taker": "0.0005",
                    "venue": "binance_usdm",
                    "tier": "regular_user",
                    "settlement_asset": "USDT",
                    "discount": "none",
                    "source": "https://www.binance.com/en/fee/trading",
                    "verified_at": "2026-08-02T09:00:00Z",
                }
            },
            "fees",
        ),
        ({"fee_policy": {"maker": "0.0002", "taker": "0.0005"}}, "venue"),
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
        ({"data_versions": {"market_frame_schema": "v0"}}, "data versions"),
        ({"fill_policy": {"broker": "exchange"}}, "fill policy"),
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


def test_live_policy_rejects_filter_snapshot_hash_mismatch() -> None:
    manifest = _live_manifest()
    manifest = replace(
        manifest,
        exchange_metadata={
            **manifest.exchange_metadata,
            "filter_snapshot_hashes": {"BTCUSDT": "f" * 64},
        },
    )

    with pytest.raises(RuntimePolicyError, match="snapshot hash"):
        LivePaperPolicy.from_manifest(manifest)
