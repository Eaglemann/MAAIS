"""Fill tracker — polls order status until terminal state.

Terminal states: FILLED, CANCELED, REJECTED, EXPIRED.
Polls with exponential backoff: 0.5s, 1s, 2s, 4s, 8s (max 5 attempts).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from maais.core.logging import get_logger
from maais.execution.binance_client import BinanceFuturesClient
from maais.execution.schemas import FillRecord, OrderSide, OrderStatus

logger = get_logger(__name__)

_TERMINAL_STATES = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
_MAX_POLLS = 5
_BASE_DELAY = 0.5


class FillTracker:
    """Polls Binance until an order reaches a terminal state."""

    def __init__(self, client: BinanceFuturesClient) -> None:
        self._client = client

    async def wait_for_fill(self, symbol: str, order_id: str) -> tuple[OrderStatus, FillRecord | None]:
        """Poll until terminal state. Returns (status, FillRecord | None).

        FillRecord is only populated when status is FILLED.
        """
        delay = _BASE_DELAY
        for attempt in range(_MAX_POLLS):
            raw = await self._client.get_order(symbol, order_id)
            status = OrderStatus(raw["status"])

            if status in _TERMINAL_STATES:
                fill = _parse_fill(raw) if status == OrderStatus.FILLED else None
                logger.info("order_terminal", order_id=order_id, status=status.value,
                            attempt=attempt + 1)
                return status, fill

            logger.debug("order_pending", order_id=order_id, status=status.value, attempt=attempt + 1)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)

        # Timeout — return last known status without fill
        logger.warning("fill_tracker_timeout", order_id=order_id, symbol=symbol)
        return OrderStatus.NEW, None


def _parse_fill(raw: dict) -> FillRecord:
    """Extract FillRecord from a FILLED Binance order response."""
    return FillRecord(
        order_id=str(raw["orderId"]),
        symbol=raw["symbol"],
        side=OrderSide(raw["side"]),
        filled_quantity=Decimal(str(raw.get("executedQty", "0"))),
        avg_fill_price=Decimal(str(raw.get("avgPrice", raw.get("price", "0")))),
        commission=Decimal(str(raw.get("commission", "0"))),
        commission_asset=raw.get("commissionAsset", "USDT"),
        fill_time=datetime.fromtimestamp(raw["updateTime"] / 1000, tz=timezone.utc),
        realized_pnl=Decimal(str(raw.get("realizedPnl", "0"))),
    )
