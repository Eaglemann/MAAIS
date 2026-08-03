"""Keyless public Binance USD-M REST adapter with mandatory contract preflight."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from maais.core.logging import get_logger
from maais.execution.paper.clock import require_utc
from maais.market_data.connectors.binance_contracts import (
    BinanceContractError,
    BinanceDepthSnapshot,
)
from maais.market_data.connectors.binance_rest_contracts import (
    BinancePublicPreflight,
    parse_closed_bar_events,
    parse_depth_snapshot,
    parse_exchange_info,
    parse_funding_events,
    parse_server_time,
)
from maais.market_data.connectors.http_transport import get_with_transport_retry
from maais.market_data.events import (
    ClosedBarPayload,
    FundingSettlementPayload,
    ObservedMarketEvent,
    VenueClockPayload,
)
from maais.market_data.schemas import FundingRateData, KlineData

logger = get_logger(__name__)

PUBLIC_FAPI_BASE_URL = "https://fapi.binance.com"
PUBLIC_SPOT_BASE_URL = "https://api.binance.com"
PUBLIC_VISION_BASE_URL = "https://data.binance.vision"
_KLINES_LIMIT = 1500
_FUNDING_LIMIT = 1000
_PROVISIONAL_WEIGHT_LIMIT = 1200
_FUNDING_CALL_LIMIT = 500
_FUNDING_WINDOW_SECONDS = 5 * 60
_DEPTH_WEIGHTS = {5: 2, 10: 2, 20: 2, 50: 2, 100: 5, 500: 10, 1000: 20}
QueryValue = str | int | float
Sleep = Callable[[float], Awaitable[None]]


class RequestPacer:
    """Conservative weighted request pacing with a runtime-updatable limit."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        monotonic: Callable[[], float],
        sleep: Sleep,
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("request pacing limit and window must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def update_limit(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("request pacing limit must be positive")
        self._limit = limit

    async def acquire(self, weight: int = 1) -> None:
        if weight <= 0:
            return
        if weight > self._limit:
            raise ValueError("single request weight exceeds the configured limit")
        async with self._lock:
            now = self._monotonic()
            if self._last_request_at is not None:
                minimum_gap = self._window_seconds * weight / self._limit
                wait = minimum_gap - (now - self._last_request_at)
                if wait > 0:
                    await self._sleep(wait)
                    now = self._monotonic()
            self._last_request_at = now


class BinanceRestConnector:
    """Public-only USD-M adapter; authenticated request material is unsupported."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        observed_now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._observed_now = observed_now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._weight = RequestPacer(
            _PROVISIONAL_WEIGHT_LIMIT,
            60,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._funding_calls = RequestPacer(
            _FUNDING_CALL_LIMIT,
            _FUNDING_WINDOW_SECONDS,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._preflight: BinancePublicPreflight | None = None

    async def __aenter__(self) -> BinanceRestConnector:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=PUBLIC_FAPI_BASE_URL,
                timeout=30.0,
                http2=True,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def request_weight_limit_per_minute(self) -> int:
        return self._weight.limit

    @property
    def preflight_complete(self) -> bool:
        return self._preflight is not None

    @property
    def preflight_result(self) -> BinancePublicPreflight:
        if self._preflight is None:
            raise RuntimeError("Binance public preflight has not completed")
        return self._preflight

    async def preflight(self, required_symbols: Sequence[str]) -> BinancePublicPreflight:
        server_raw, server_observed = await self._get_json("/fapi/v1/time", {}, weight=1)
        clock = parse_server_time(server_raw, observed_at=server_observed)
        assert isinstance(clock.payload, VenueClockPayload)
        exchange_raw, exchange_observed = await self._get_json(
            "/fapi/v1/exchangeInfo",
            {},
            weight=1,
        )
        result = parse_exchange_info(
            exchange_raw,
            required_symbols=required_symbols,
            server_time=clock.payload.server_time,
            server_observed_at=server_observed,
            observed_at=exchange_observed,
        )
        self._weight.update_limit(result.request_weight_limit_per_minute)
        self._preflight = result
        return result

    async def ping(self) -> bool:
        try:
            raw, _ = await self._get_json("/fapi/v1/ping", {}, weight=1)
            return isinstance(raw, dict) and not raw
        except Exception as exc:
            logger.warning("ping_failed", error=str(exc))
            return False

    async def get_depth_snapshot(
        self,
        symbol: str,
        *,
        limit: int = 1000,
    ) -> BinanceDepthSnapshot:
        self._require_preflight_symbol(symbol)
        try:
            weight = _DEPTH_WEIGHTS[limit]
        except KeyError as exc:
            raise ValueError(f"unsupported Binance depth limit: {limit}") from exc
        raw, observed_at = await self._get_json(
            "/fapi/v1/depth",
            {"symbol": symbol, "limit": limit},
            weight=weight,
        )
        return parse_depth_snapshot(raw, symbol=symbol, observed_at=observed_at)

    async def get_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        limit: int = _KLINES_LIMIT,
    ) -> tuple[ObservedMarketEvent, ...]:
        self._require_preflight_symbol(symbol)
        require_utc(start, "start")
        require_utc(end, "end")
        if end <= start:
            raise ValueError("closed-bar end must follow start")
        _validate_kline_limit(limit)
        raw, observed_at = await self._get_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": _dt_to_ms(start),
                "endTime": _dt_to_ms(end) - 1,
                "limit": limit,
            },
            weight=_kline_weight(limit),
        )
        return parse_closed_bar_events(
            raw,
            symbol=symbol,
            interval=interval,
            observed_at=observed_at,
        )

    async def iter_closed_bar_events(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> AsyncGenerator[tuple[ObservedMarketEvent, ...], None]:
        cursor = start
        while cursor < end:
            batch = await self.get_closed_bar_events(symbol, interval, cursor, end)
            if not batch:
                break
            yield batch
            last = batch[-1].payload
            if not isinstance(last, ClosedBarPayload):
                raise RuntimeError("closed-bar adapter produced an invalid payload")
            next_cursor = last.bar_close_at
            if next_cursor <= cursor:
                raise RuntimeError("closed-bar pagination did not advance")
            cursor = next_cursor

    async def get_funding_events(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
        page_limit: int = _FUNDING_LIMIT,
    ) -> tuple[ObservedMarketEvent, ...]:
        self._require_preflight_symbol(symbol)
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("funding range must be nonnegative and ascending")
        if not 1 <= page_limit <= _FUNDING_LIMIT:
            raise ValueError("funding page_limit must be in [1, 1000]")
        cursor = start_ms
        events: list[ObservedMarketEvent] = []
        while cursor <= end_ms:
            await self._funding_calls.acquire()
            raw, observed_at = await self._get_json(
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": page_limit,
                },
                weight=0,
            )
            page = parse_funding_events(raw, symbol=symbol, observed_at=observed_at)
            if not page:
                break
            page_times = tuple(
                _dt_to_ms(event.payload.funding_at)
                for event in page
                if isinstance(event.payload, FundingSettlementPayload)
            )
            if len(page_times) != len(page) or any(
                funding_ms < cursor or funding_ms > end_ms for funding_ms in page_times
            ):
                raise BinanceContractError(
                    "funding response returned an event outside the requested window"
                )
            events.extend(page)
            last = page[-1].payload
            assert isinstance(last, FundingSettlementPayload)
            next_cursor = _dt_to_ms(last.funding_at) + 1
            if next_cursor <= cursor:
                raise RuntimeError("funding pagination did not advance")
            cursor = next_cursor
            if len(page) < page_limit:
                break
        identities = [event.event_id for event in events]
        if len(set(identities)) != len(identities):
            raise BinanceContractError("funding pagination returned duplicate settlements")
        return tuple(events)

    # Legacy historical-ingestion compatibility. New official execution code uses
    # the ObservedMarketEvent methods above.
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = _KLINES_LIMIT,
    ) -> list[KlineData]:
        _validate_kline_limit(limit)
        raw, _ = await self._get_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
            weight=_kline_weight(limit),
        )
        if not isinstance(raw, list) or not all(isinstance(row, list) for row in raw):
            raise BinanceContractError("expected a list of kline rows")
        return [_parse_legacy_kline_row(row, symbol, interval) for row in raw]

    async def iter_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> AsyncGenerator[list[KlineData], None]:
        cursor_ms = _dt_to_ms(start)
        end_ms = _dt_to_ms(end)
        while cursor_ms < end_ms:
            batch = await self.get_klines(symbol, interval, cursor_ms, end_ms)
            if not batch:
                break
            yield batch
            next_cursor = _dt_to_ms(batch[-1].close_time) + 1
            if next_cursor <= cursor_ms:
                raise RuntimeError("legacy kline pagination did not advance")
            cursor_ms = next_cursor

    async def get_funding_rates(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> list[FundingRateData]:
        events = await self.get_funding_events(
            symbol,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        results: list[FundingRateData] = []
        for event in events:
            payload = event.payload
            assert isinstance(payload, FundingSettlementPayload)
            results.append(
                FundingRateData(
                    symbol=event.symbol,
                    funding_time=payload.funding_at,
                    funding_rate=payload.funding_rate,
                    mark_price=payload.mark_price,
                )
            )
        return results

    async def get_mark_price(self, symbol: str) -> Decimal:
        self._require_preflight_symbol(symbol)
        data, _ = await self._get_json(
            "/fapi/v1/premiumIndex",
            {"symbol": symbol},
            weight=1,
        )
        if not isinstance(data, dict) or not isinstance(data.get("markPrice"), str):
            raise BinanceContractError("expected a mark-price object with decimal string")
        value = Decimal(data["markPrice"])
        if not value.is_finite() or value <= 0:
            raise BinanceContractError("mark price must be positive and finite")
        return value

    async def get_spot_price(self, symbol: str) -> Decimal:
        async with httpx.AsyncClient(base_url=PUBLIC_SPOT_BASE_URL, timeout=10.0) as spot:
            response = await spot.get("/api/v3/ticker/price", params={"symbol": symbol})
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("price"), str):
            raise BinanceContractError("expected a spot-price object with decimal string")
        value = Decimal(data["price"])
        if not value.is_finite() or value <= 0:
            raise BinanceContractError("spot price must be positive and finite")
        return value

    async def download_monthly_klines_csv(
        self,
        symbol: str,
        interval: str,
        year: int,
        month: int,
    ) -> list[KlineData] | None:
        filename = f"{symbol}-{interval}-{year}-{month:02d}.zip"
        url = (
            f"{PUBLIC_VISION_BASE_URL}/data/futures/um/monthly/klines/"
            f"{symbol}/{interval}/{filename}"
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url)
            if response.status_code == 404:
                logger.debug("bulk_file_not_found", url=url)
                return None
            response.raise_for_status()
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                names = archive.namelist()
                if len(names) != 1:
                    raise BinanceContractError("monthly kline archive must contain one CSV")
                with archive.open(names[0]) as csv_file:
                    lines = csv_file.read().decode().splitlines()
        except (UnicodeDecodeError, zipfile.BadZipFile) as exc:
            raise BinanceContractError("monthly kline archive is invalid") from exc
        candles: list[KlineData] = []
        for line in lines:
            if not line or line.startswith("open_time"):
                continue
            candles.append(_parse_legacy_kline_row(line.split(","), symbol, interval))
        return candles

    def _require_preflight_symbol(self, symbol: str) -> None:
        preflight = self.preflight_result
        if symbol not in {item.symbol for item in preflight.exchange_filters}:
            raise RuntimeError(f"symbol was not admitted by Binance public preflight: {symbol}")

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, QueryValue],
        *,
        weight: int,
    ) -> tuple[object, datetime]:
        client = self._http
        await self._weight.acquire(weight)
        response = await get_with_transport_retry(
            client,
            path,
            params,
            component="binance_usdm",
            sleep=self._sleep,
        )
        response.raise_for_status()
        observed_at = self._observed_now()
        require_utc(observed_at, "observed_now")
        try:
            return response.json(), observed_at
        except ValueError as exc:
            raise BinanceContractError(f"{path} did not return valid JSON") from exc

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("BinanceRestConnector must be used as an async context manager")
        return self._client


def _validate_kline_limit(limit: int) -> None:
    if not 1 <= limit <= _KLINES_LIMIT:
        raise ValueError("kline limit must be in [1, 1500]")


def _kline_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def _parse_legacy_kline_row(
    row: Sequence[object],
    symbol: str,
    timeframe: str,
) -> KlineData:
    if len(row) != 12:
        raise BinanceContractError("legacy kline row must contain exactly 12 fields")
    try:
        return KlineData(
            symbol=symbol,
            timeframe=timeframe,
            open_time=_ms_to_dt(_legacy_integer(row[0], "open_time")),
            open=_legacy_decimal(row[1], "open"),
            high=_legacy_decimal(row[2], "high"),
            low=_legacy_decimal(row[3], "low"),
            close=_legacy_decimal(row[4], "close"),
            volume=_legacy_decimal(row[5], "volume"),
            close_time=_ms_to_dt(_legacy_integer(row[6], "close_time")),
            quote_volume=_legacy_decimal(row[7], "quote_volume"),
            trade_count=_legacy_integer(row[8], "trade_count"),
            taker_buy_volume=_legacy_decimal(row[9], "taker_buy_volume"),
            taker_buy_quote_volume=_legacy_decimal(row[10], "taker_buy_quote_volume"),
            is_closed=True,
        )
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise BinanceContractError("legacy kline row contains an invalid field") from exc


def _legacy_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BinanceContractError(f"legacy kline {field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise BinanceContractError(f"legacy kline {field} must be an integer") from exc


def _legacy_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise BinanceContractError(f"legacy kline {field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise BinanceContractError(f"legacy kline {field} must be finite")
    return parsed


def _ms_to_dt(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def _dt_to_ms(value: datetime) -> int:
    require_utc(value, "datetime")
    return int(value.timestamp() * 1000)
