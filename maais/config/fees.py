"""Frozen exchange-fee assumptions used by official paper candidates."""

from decimal import Decimal

BINANCE_USDM_REGULAR_MAKER_FEE_RATE = Decimal("0.0002")
BINANCE_USDM_REGULAR_TAKER_FEE_RATE = Decimal("0.0005")
BINANCE_FEE_SCHEDULE_URL = "https://www.binance.com/en/fee/trading"
BINANCE_FEE_SCHEDULE_VERIFIED_AT = "2026-08-02T22:10:00Z"
