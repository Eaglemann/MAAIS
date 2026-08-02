"""Binance Futures REST connector.

Handles:
  - Historical klines (OHLCV) via GET /fapi/v1/klines
  - Funding rate history via GET /fapi/v1/fundingRate
  - Mark price snapshot via GET /fapi/v1/premiumIndex
  - Bulk historical CSV download from data.binance.vision

Rate limiting: Binance Futures allows 2400 weight/minute.
This connector uses a simple token-bucket limiter at a conservative
1200 weight/minute to leave headroom for other API calls.
"""

import asyncio
import io
import zipfile
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from maais.core.logging import get_logger
from maais.market_data.schemas import FundingRateData, KlineData

logger = get_logger(__name__)

_FAPI_BASE = "https://fapi.binance.com"
_VISION_BASE = "https://data.binance.vision"
_KLINES_LIMIT = 1500  # max candles per REST request
_FUNDING_LIMIT = 1000  # max funding rate records per request
_RATE_LIMIT_WEIGHT_PER_MIN = 1200  # conservative (actual limit: 2400)
_WEIGHT_INTERVAL = 60 / _RATE_LIMIT_WEIGHT_PER_MIN  # seconds per weight unit
QueryValue = str | int | float


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _parse_kline_row(row: list, symbol: str, timeframe: str) -> KlineData:
    return KlineData(
        symbol=symbol,
        timeframe=timeframe,
        open_time=_ms_to_dt(int(row[0])),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
        close_time=_ms_to_dt(int(row[6])),
        quote_volume=Decimal(str(row[7])),
        trade_count=int(row[8]),
        taker_buy_volume=Decimal(str(row[9])),
        taker_buy_quote_volume=Decimal(str(row[10])),
        is_closed=True,
    )


class BinanceRestConnector:
    """Async Binance Futures REST API connector with rate limiting."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._last_request_time: float = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "BinanceRestConnector":
        self._client = httpx.AsyncClient(
            base_url=_FAPI_BASE,
            timeout=30.0,
            http2=True,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()

    async def _throttle(self, weight: int = 1) -> None:
        """Token-bucket throttle: wait if needed to respect rate limit."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            required_gap = _WEIGHT_INTERVAL * weight
            elapsed = now - self._last_request_time
            if elapsed < required_gap:
                await asyncio.sleep(required_gap - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()

    async def _get(self, path: str, params: dict[str, QueryValue], weight: int = 2) -> object:
        assert self._client, "Use as async context manager"
        await self._throttle(weight)
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def ping(self) -> bool:
        """Check connectivity to Binance Futures API."""
        try:
            assert self._client
            await self._throttle(1)
            resp = await self._client.get("/fapi/v1/ping")
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("ping_failed", error=str(exc))
            return False

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = _KLINES_LIMIT,
    ) -> list[KlineData]:
        """Fetch up to `limit` 1m candles in one API call."""
        raw = await self._get(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
            weight=2,
        )
        if not isinstance(raw, list) or not all(isinstance(row, list) for row in raw):
            raise TypeError("expected a list of kline rows")
        return [_parse_kline_row(row, symbol, interval) for row in raw]

    async def iter_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> AsyncGenerator[list[KlineData], None]:
        """Paginate through the full [start, end) range, yielding batches."""
        cursor_ms = _dt_to_ms(start)
        end_ms = _dt_to_ms(end)

        while cursor_ms < end_ms:
            batch = await self.get_klines(symbol, interval, cursor_ms, end_ms)
            if not batch:
                break
            yield batch
            # Advance past the last candle's close_time
            cursor_ms = _dt_to_ms(batch[-1].close_time) + 1
            logger.debug(
                "klines_page_fetched",
                symbol=symbol,
                count=len(batch),
                up_to=batch[-1].open_time.isoformat(),
            )

    async def get_funding_rates(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRateData]:
        """Fetch all funding rate records in [start_ms, end_ms]."""
        results: list[FundingRateData] = []
        cursor = start_ms

        while cursor < end_ms:
            raw = await self._get(
                "/fapi/v1/fundingRate",
                {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": _FUNDING_LIMIT},
                weight=1,
            )
            if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
                raise TypeError("expected a list of funding-rate objects")
            if not raw:
                break
            for item in raw:
                results.append(
                    FundingRateData(
                        symbol=item["symbol"],
                        funding_time=_ms_to_dt(int(item["fundingTime"])),
                        funding_rate=Decimal(str(item["fundingRate"])),
                        mark_price=Decimal(str(item.get("markPrice", "0"))),
                    )
                )
            cursor = int(raw[-1]["fundingTime"]) + 1

        return results

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Current mark price for cross-exchange divergence check (Rule 16)."""
        data = await self._get("/fapi/v1/premiumIndex", {"symbol": symbol}, weight=1)
        if not isinstance(data, dict):
            raise TypeError("expected a mark-price object")
        return Decimal(str(data["markPrice"]))

    async def get_spot_price(self, symbol: str) -> Decimal:
        """Spot price via Binance spot API for cross-exchange comparison (Rule 16).

        Uses a separate httpx call since the base URL is different.
        """
        async with httpx.AsyncClient(base_url="https://api.binance.com", timeout=10.0) as spot:
            resp = await spot.get("/api/v3/ticker/price", params={"symbol": symbol})
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise TypeError("expected a spot-price object")
            return Decimal(str(data["price"]))

    # ── Bulk download from data.binance.vision ────────────────────────────────

    async def download_monthly_klines_csv(
        self,
        symbol: str,
        interval: str,
        year: int,
        month: int,
    ) -> list[KlineData] | None:
        """Download a monthly kline CSV from data.binance.vision.

        Returns None if the file does not exist (month not yet available).
        Much faster than REST pagination for historical backfill.
        """
        filename = f"{symbol}-{interval}-{year}-{month:02d}.zip"
        url = f"{_VISION_BASE}/data/futures/um/monthly/klines/{symbol}/{interval}/{filename}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                logger.debug("bulk_file_not_found", url=url)
                return None
            resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = zf.namelist()[0]
            with zf.open(csv_name) as csv_file:
                lines = csv_file.read().decode().splitlines()

        candles: list[KlineData] = []
        for line in lines:
            if not line or line.startswith("open_time"):
                continue
            row = line.split(",")
            candles.append(_parse_kline_row(row, symbol, interval))

        logger.info(
            "bulk_download_complete",
            symbol=symbol,
            year=year,
            month=month,
            candles=len(candles),
        )
        return candles
