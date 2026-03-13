"""Historical data ingestor for the Market Data Layer.

Two-phase ingestion strategy:
  1. Bulk download  — data.binance.vision monthly CSV files (fast, no rate limit)
  2. REST API fill  — fills any gaps not covered by bulk files (recent months)

Data is stored in:
  - DuckDB   — full historical store; fast columnar queries for agents
  - PostgreSQL — canonical record; recent data + funding rates

Usage:
    async with BinanceRestConnector() as rest:
        ingestor = HistoricalIngestor(rest)
        await ingestor.ingest_all(start_year=2021, start_month=1)
"""

import asyncio
from datetime import datetime, timezone

import duckdb

from maais.config.constants import (
    AGGREGATION_TIMEFRAMES,
    HISTORICAL_DEPTH_YEARS,
    PRIMARY_TIMEFRAME,
    TRADING_PAIRS,
)
from maais.config.settings import get_settings
from maais.core.logging import get_logger
from maais.db.connection import get_session
from maais.market_data.aggregator import aggregate_klines
from maais.market_data.connectors.binance_rest import BinanceRestConnector
from maais.market_data.integrity.validator import DataIntegrityValidator
from maais.market_data.models import FundingRate, Kline
from maais.market_data.schemas import FundingRateData, KlineData

logger = get_logger(__name__)


def _ensure_duckdb_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines (
            symbol      VARCHAR NOT NULL,
            timeframe   VARCHAR NOT NULL,
            open_time   TIMESTAMPTZ NOT NULL,
            open        DECIMAL(20, 8),
            high        DECIMAL(20, 8),
            low         DECIMAL(20, 8),
            close       DECIMAL(20, 8),
            volume      DECIMAL(30, 8),
            close_time  TIMESTAMPTZ,
            quote_volume        DECIMAL(30, 8),
            trade_count         INTEGER,
            taker_buy_volume    DECIMAL(30, 8),
            taker_buy_quote_volume DECIMAL(30, 8),
            PRIMARY KEY (symbol, timeframe, open_time)
        )
    """)


def _kline_to_row(k: KlineData) -> tuple:
    return (
        k.symbol, k.timeframe, k.open_time,
        k.open, k.high, k.low, k.close, k.volume,
        k.close_time, k.quote_volume, k.trade_count,
        k.taker_buy_volume, k.taker_buy_quote_volume,
    )


class HistoricalIngestor:
    def __init__(self, rest: BinanceRestConnector) -> None:
        self._rest = rest
        self._validator = DataIntegrityValidator()
        settings = get_settings()
        self._duck = duckdb.connect(settings.duckdb_path)
        _ensure_duckdb_schema(self._duck)

    def close(self) -> None:
        self._duck.close()

    # ── Public interface ──────────────────────────────────────────────────────

    async def ingest_all(
        self,
        start_year: int | None = None,
        start_month: int = 1,
        symbols: tuple[str, ...] = TRADING_PAIRS,
    ) -> None:
        """Ingest all symbols from start_year (defaults to HISTORICAL_DEPTH_YEARS ago)."""
        now = datetime.now(timezone.utc)
        if start_year is None:
            start_year = now.year - HISTORICAL_DEPTH_YEARS

        for symbol in symbols:
            logger.info("ingest_start", symbol=symbol, start=f"{start_year}-{start_month:02d}")
            await self.ingest_symbol(symbol, start_year, start_month)

    async def ingest_symbol(self, symbol: str, start_year: int, start_month: int) -> None:
        """Ingest one symbol: bulk files first, then REST for recent gaps."""
        now = datetime.now(timezone.utc)
        total_candles = 0

        # Phase 1: bulk monthly downloads
        year, month = start_year, start_month
        while (year, month) < (now.year, now.month):
            candles = await self._rest.download_monthly_klines_csv(
                symbol, PRIMARY_TIMEFRAME, year, month
            )
            if candles:
                await self._store_candles(candles)
                total_candles += len(candles)

            month += 1
            if month > 12:
                month = 1
                year += 1
            await asyncio.sleep(0.1)  # brief pause between downloads

        # Phase 2: REST fill for current month (bulk not yet available)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        async for batch in self._rest.iter_klines(symbol, PRIMARY_TIMEFRAME, month_start, now):
            results = self._validator.validate_candles(batch, PRIMARY_TIMEFRAME)
            if not self._validator.all_passed(results):
                logger.warning("validation_failed_on_rest_batch", symbol=symbol)
            await self._store_candles(batch)
            total_candles += len(batch)

        # Phase 3: ingest funding rate history
        start_dt = datetime(start_year, start_month, 1, tzinfo=timezone.utc)
        funding = await self._rest.get_funding_rates(
            symbol,
            int(start_dt.timestamp() * 1000),
            int(now.timestamp() * 1000),
        )
        await self._store_funding_rates(funding)

        logger.info("ingest_complete", symbol=symbol, candles=total_candles, funding_records=len(funding))

    # ── Storage helpers ───────────────────────────────────────────────────────

    async def _store_candles(self, candles_1m: list[KlineData]) -> None:
        """Persist 1m candles + all aggregated timeframes to DuckDB and PostgreSQL."""
        all_candles: list[KlineData] = list(candles_1m)
        for tf in AGGREGATION_TIMEFRAMES:
            all_candles.extend(aggregate_klines(candles_1m, tf))

        # DuckDB: bulk upsert
        rows = [_kline_to_row(k) for k in all_candles]
        self._duck.executemany(
            """INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

        # PostgreSQL: upsert via ORM
        async with get_session() as session:
            for k in all_candles:
                kline = Kline(
                    symbol=k.symbol,
                    timeframe=k.timeframe,
                    open_time=k.open_time,
                    open=k.open,
                    high=k.high,
                    low=k.low,
                    close=k.close,
                    volume=k.volume,
                    close_time=k.close_time,
                    quote_volume=k.quote_volume,
                    trade_count=k.trade_count,
                    taker_buy_volume=k.taker_buy_volume,
                    taker_buy_quote_volume=k.taker_buy_quote_volume,
                )
                await session.merge(kline)

    async def _store_funding_rates(self, rates: list[FundingRateData]) -> None:
        async with get_session() as session:
            for r in rates:
                record = FundingRate(
                    symbol=r.symbol,
                    funding_time=r.funding_time,
                    funding_rate=r.funding_rate,
                    mark_price=r.mark_price,
                )
                await session.merge(record)
