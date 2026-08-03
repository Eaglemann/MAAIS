"""Frozen data and fill identities for the official local paper candidate."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

GOLDEN_PAPER_REPLAY_SHA256 = (
    "4d2dec967a8fd98ba04616b834c1b247442af3b168409ba6d45bc24833e6b5cc"  # pragma: allowlist secret
)

OFFICIAL_DATA_VERSIONS: Mapping[str, str] = MappingProxyType(
    {
        "input_kind": "public_live_observations",
        "decision_timeframe": "1m",
        "market_frame_schema": "v1",
        "event_ledger_schema": "v1",
        "futures_contract": "binance_usdm_public_v1",
        "primary_spot_contract": "binance_spot_public_v1",
        "secondary_spot_contract": "bybit_spot_public_v1",
        "golden_replay_sha256": GOLDEN_PAPER_REPLAY_SHA256,
    }
)

OFFICIAL_FILL_POLICY: Mapping[str, str] = MappingProxyType(
    {
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
    }
)

OFFICIAL_MAINTENANCE_MARGIN_RATE = Decimal("0.005")

OFFICIAL_MARGIN_POLICY: Mapping[str, str | bool] = MappingProxyType(
    {
        "maintenance_margin_model": "fixed_fraction_of_gross_notional",
        "maintenance_margin_rate": str(OFFICIAL_MAINTENANCE_MARGIN_RATE),
        "liquidation_price_model": "not_modeled",
        "exchange_liquidation_parity": False,
    }
)

OFFICIAL_MODEL_LIMITATIONS = ("exchange_liquidation_behavior_not_modeled",)
