"""Funding payment tracker.

Fetches funding payments from Binance and records them against
open positions via the compliance trade logger (Rule 8, field: funding_paid_received).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from maais.core.logging import get_logger
from maais.execution.binance_client import BinanceFuturesClient

logger = get_logger(__name__)


class FundingTracker:
    """Tracks funding payments for open positions."""

    def __init__(self, client: BinanceFuturesClient) -> None:
        self._client = client
        # symbol → cumulative funding paid/received (negative = paid, positive = received)
        self._cumulative: dict[str, Decimal] = {}

    async def refresh(self, symbol: str) -> Decimal:
        """Fetch latest funding payments from Binance and update cumulative total.

        Returns the net funding (positive = received, negative = paid).
        """
        payments = await self._client.get_funding_payments(symbol, limit=50)
        total = Decimal("0")
        for p in payments:
            total += Decimal(str(p.get("income", "0")))

        self._cumulative[symbol] = total
        logger.info("funding_refreshed", symbol=symbol, total=str(total))
        return total

    def get_cumulative(self, symbol: str) -> Decimal:
        """Return last known cumulative funding for a symbol (0 if never fetched)."""
        return self._cumulative.get(symbol, Decimal("0"))
