"""DuckDB-backed feature store.

Persists FeatureSet objects for fast agent queries over historical features.
Schema mirrors FeatureSet fields exactly.
"""

from datetime import datetime

import duckdb

from maais.config.settings import get_settings
from maais.core.logging import get_logger
from maais.feature_pipeline.features import FeatureSet

logger = get_logger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS features (
    symbol               VARCHAR NOT NULL,
    timeframe            VARCHAR NOT NULL,
    timestamp            TIMESTAMPTZ NOT NULL,
    zscore               DOUBLE,
    zscore_mean          DOUBLE,
    zscore_std           DOUBLE,
    ema_fast             DOUBLE,
    ema_slow             DOUBLE,
    ema_signal           DOUBLE,
    roc_short            DOUBLE,
    roc_long             DOUBLE,
    atr                  DOUBLE,
    rolling_std          DOUBLE,
    bid_ask_spread       DOUBLE,
    book_imbalance       DOUBLE,
    funding_rate         DOUBLE,
    annualized_funding   DOUBLE,
    funding_bias         VARCHAR,
    regime               VARCHAR,
    PRIMARY KEY (symbol, timeframe, timestamp)
)
"""

_UPSERT = """
INSERT OR REPLACE INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _to_row(f: FeatureSet) -> tuple:
    return (
        f.symbol, f.timeframe, f.timestamp,
        f.zscore, f.zscore_mean, f.zscore_std,
        f.ema_fast, f.ema_slow, f.ema_signal,
        f.roc_short, f.roc_long,
        f.atr, f.rolling_std,
        f.bid_ask_spread, f.book_imbalance,
        f.funding_rate, f.annualized_funding, f.funding_bias,
        f.regime,
    )


class FeatureStore:
    def __init__(self, duckdb_path: str | None = None) -> None:
        path = duckdb_path or get_settings().duckdb_path
        self._conn = duckdb.connect(path)
        self._conn.execute(_CREATE_TABLE)

    def close(self) -> None:
        self._conn.close()

    def save(self, features: list[FeatureSet]) -> None:
        if not features:
            return
        rows = [_to_row(f) for f in features]
        self._conn.executemany(_UPSERT, rows)
        logger.debug("features_saved", count=len(features))

    def get_latest(self, symbol: str, timeframe: str) -> FeatureSet | None:
        result = self._conn.execute(
            "SELECT * FROM features WHERE symbol=? AND timeframe=? ORDER BY timestamp DESC LIMIT 1",
            [symbol, timeframe],
        ).fetchone()
        return _row_to_feature(result) if result else None

    def get_range(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[FeatureSet]:
        rows = self._conn.execute(
            "SELECT * FROM features WHERE symbol=? AND timeframe=? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            [symbol, timeframe, start, end],
        ).fetchall()
        return [_row_to_feature(r) for r in rows]


def _row_to_feature(row: tuple) -> FeatureSet:
    return FeatureSet(
        symbol=row[0],
        timeframe=row[1],
        timestamp=row[2],
        zscore=row[3],
        zscore_mean=row[4],
        zscore_std=row[5],
        ema_fast=row[6],
        ema_slow=row[7],
        ema_signal=row[8],
        roc_short=row[9],
        roc_long=row[10],
        atr=row[11],
        rolling_std=row[12],
        bid_ask_spread=row[13],
        book_imbalance=row[14],
        funding_rate=row[15],
        annualized_funding=row[16],
        funding_bias=row[17],
        regime=row[18],
    )
