"""Exchange rate fetcher for trade record field 10 (Rule 8).

Converts USDT → local currency at execution time.

Strategy:
  1. If local_currency == "USD" or "USDT": rate = 1.0 (no conversion needed)
  2. Otherwise: query Binance spot API for {LOCAL_CURRENCY}USDT pair
     e.g. EURUSDT = 1.08 means 1 EUR = 1.08 USDT → rate = 1/1.08

Configuration:
  Set LOCAL_CURRENCY in .env (default: "USD").
  Add to settings.py as needed.
"""

from decimal import Decimal

import httpx

from maais.core.logging import get_logger

logger = get_logger(__name__)

_SPOT_API = "https://api.binance.com/api/v3/ticker/price"
_STABLECOINS = {"USD", "USDT", "USDC", "BUSD"}


class ExchangeRateFetcher:
    """Fetches USDT → local currency exchange rate at trade execution time."""

    def __init__(self, local_currency: str = "USD") -> None:
        self._local = local_currency.upper()

    async def get_rate(self) -> Decimal:
        """Return the current USDT → local_currency rate.

        For USD/USDT: always 1.0.
        For others: queries Binance spot.
        """
        if self._local in _STABLECOINS:
            return Decimal("1.0")

        symbol = f"{self._local}USDT"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(_SPOT_API, params={"symbol": symbol})
                resp.raise_for_status()
                usdt_per_local = Decimal(resp.json()["price"])
                # EURUSDT = 1.08 → 1 EUR = 1.08 USDT → 1 USDT = 1/1.08 EUR
                return (Decimal("1") / usdt_per_local).quantize(Decimal("0.000001"))
        except Exception as exc:
            logger.error(
                "exchange_rate_fetch_failed",
                local_currency=self._local,
                symbol=symbol,
                error=str(exc),
            )
            # Fallback: use 1.0 and log the failure.
            # The trade record will be created with an approximate rate.
            # Monitoring layer (Batch 8) should alert on repeated failures.
            return Decimal("1.0")
