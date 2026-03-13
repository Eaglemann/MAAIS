"""Data Integrity Layer — Rule 16 (BEHAVIOURS.md).

Six mandatory validation checks on all incoming market data:
  1. Missing-data detection
  2. Price-outlier filtering
  3. Cross-exchange comparison
  4. Timestamp synchronization
  5. API outage detection
  6. Historical dataset validation

Each check returns a ValidationResult. A failed check logs a warning
and propagates to the Monitoring Layer (Batch 8) via structured logs.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from maais.config.constants import (
    API_OUTAGE_TIMEOUT_SECONDS,
    CROSS_EXCHANGE_MAX_DIVERGENCE,
    PRICE_OUTLIER_Z_THRESHOLD,
    TIMEFRAME_MINUTES,
)
from maais.core.logging import get_logger
from maais.market_data.schemas import KlineData

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    check: str
    passed: bool
    symbol: str
    details: str = ""

    def __post_init__(self) -> None:
        if not self.passed:
            logger.warning(
                "integrity_check_failed",
                check=self.check,
                symbol=self.symbol,
                details=self.details,
            )


class DataIntegrityValidator:
    """Runs all six Rule 16 integrity checks against market data."""

    # ── Check 1: Missing-data detection ──────────────────────────────────────

    def check_missing_data(
        self,
        candles: list[KlineData],
        timeframe: str,
    ) -> ValidationResult:
        """Detect gaps larger than one interval in the candle sequence."""
        if len(candles) < 2:
            return ValidationResult("missing_data", True, candles[0].symbol if candles else "")

        symbol = candles[0].symbol
        interval_td = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        gaps: list[str] = []

        for i in range(1, len(candles)):
            expected = candles[i - 1].open_time + interval_td
            actual = candles[i].open_time
            if actual > expected + timedelta(seconds=30):  # tolerance: 30s for clock drift
                gap_minutes = int((actual - expected).total_seconds() / 60)
                gaps.append(f"{expected.isoformat()} → {actual.isoformat()} ({gap_minutes}m gap)")

        if gaps:
            return ValidationResult(
                "missing_data", False, symbol,
                f"{len(gaps)} gap(s) detected: {'; '.join(gaps[:3])}{'...' if len(gaps) > 3 else ''}",
            )
        return ValidationResult("missing_data", True, symbol)

    # ── Check 2: Price-outlier filtering ─────────────────────────────────────

    def check_price_outliers(
        self,
        candles: list[KlineData],
        z_threshold: float = PRICE_OUTLIER_Z_THRESHOLD,
    ) -> ValidationResult:
        """Flag candles where the close-to-close change exceeds z_threshold standard deviations."""
        if len(candles) < 3:
            return ValidationResult("price_outliers", True, candles[0].symbol if candles else "")

        symbol = candles[0].symbol
        changes = [
            float(candles[i].close - candles[i - 1].close) / float(candles[i - 1].close)
            for i in range(1, len(candles))
        ]

        mean = statistics.mean(changes)
        stdev = statistics.stdev(changes)
        if stdev == 0:
            return ValidationResult("price_outliers", True, symbol)

        outliers = [
            f"index={i+1} close={candles[i+1].close} Z={abs((changes[i] - mean) / stdev):.2f}"
            for i, c in enumerate(changes)
            if stdev > 0 and abs((c - mean) / stdev) > z_threshold
        ]

        if outliers:
            return ValidationResult(
                "price_outliers", False, symbol,
                f"{len(outliers)} outlier(s): {'; '.join(outliers[:3])}",
            )
        return ValidationResult("price_outliers", True, symbol)

    # ── Check 3: Cross-exchange comparison ───────────────────────────────────

    def check_cross_exchange_divergence(
        self,
        symbol: str,
        futures_price: Decimal,
        spot_price: Decimal,
        threshold: float = CROSS_EXCHANGE_MAX_DIVERGENCE,
    ) -> ValidationResult:
        """Compare futures mark price vs Binance spot. Divergence > threshold triggers warning."""
        if spot_price == 0:
            return ValidationResult("cross_exchange", True, symbol, "spot_price=0 skipped")

        divergence = abs(float(futures_price - spot_price) / float(spot_price))
        if divergence > threshold:
            return ValidationResult(
                "cross_exchange", False, symbol,
                f"divergence={divergence:.4%} futures={futures_price} spot={spot_price}",
            )
        return ValidationResult("cross_exchange", True, symbol)

    # ── Check 4: Timestamp synchronization ───────────────────────────────────

    def check_timestamp_sync(
        self,
        candles: list[KlineData],
        timeframe: str,
    ) -> ValidationResult:
        """Validate that each candle's open_time is exactly one interval after the previous."""
        if len(candles) < 2:
            return ValidationResult("timestamp_sync", True, candles[0].symbol if candles else "")

        symbol = candles[0].symbol
        interval_td = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        bad: list[str] = []

        for i in range(1, len(candles)):
            expected = candles[i - 1].open_time + interval_td
            actual = candles[i].open_time
            delta_s = abs((actual - expected).total_seconds())
            if delta_s > 1:  # allow 1-second tolerance for clock drift
                bad.append(f"index={i} expected={expected.isoformat()} got={actual.isoformat()}")

        if bad:
            return ValidationResult(
                "timestamp_sync", False, symbol,
                f"{len(bad)} misaligned timestamp(s): {'; '.join(bad[:3])}",
            )
        return ValidationResult("timestamp_sync", True, symbol)

    # ── Check 5: API outage detection ────────────────────────────────────────

    def check_api_outage(
        self,
        symbol: str,
        last_received_at: datetime,
        timeout_seconds: int = API_OUTAGE_TIMEOUT_SECONDS,
    ) -> ValidationResult:
        """Detect stale data: no message received within timeout window."""
        now = datetime.now(timezone.utc)
        age_seconds = (now - last_received_at).total_seconds()
        if age_seconds > timeout_seconds:
            return ValidationResult(
                "api_outage", False, symbol,
                f"no data for {age_seconds:.0f}s (threshold={timeout_seconds}s)",
            )
        return ValidationResult("api_outage", True, symbol)

    # ── Check 6: Historical dataset validation ────────────────────────────────

    def check_historical_completeness(
        self,
        candles: list[KlineData],
        start: datetime,
        end: datetime,
        timeframe: str,
    ) -> ValidationResult:
        """Verify coverage: actual candle count vs expected count for the date range."""
        if not candles:
            return ValidationResult(
                "historical_completeness", False, "",
                f"no candles returned for {start.isoformat()} → {end.isoformat()}",
            )

        symbol = candles[0].symbol
        interval_minutes = TIMEFRAME_MINUTES[timeframe]
        total_minutes = (end - start).total_seconds() / 60
        expected_count = int(total_minutes / interval_minutes)
        actual_count = len(candles)

        # Allow up to 1% missing (e.g. maintenance windows, public holidays with low volume)
        coverage = actual_count / expected_count if expected_count else 1.0
        if coverage < 0.99:
            return ValidationResult(
                "historical_completeness", False, symbol,
                f"coverage={coverage:.2%} actual={actual_count} expected≈{expected_count}",
            )
        return ValidationResult("historical_completeness", True, symbol)

    # ── Run all checks ────────────────────────────────────────────────────────

    def validate_candles(
        self,
        candles: list[KlineData],
        timeframe: str,
    ) -> list[ValidationResult]:
        """Run checks 1, 2, 4 against a candle batch. Returns all results."""
        return [
            self.check_missing_data(candles, timeframe),
            self.check_price_outliers(candles),
            self.check_timestamp_sync(candles, timeframe),
        ]

    def all_passed(self, results: list[ValidationResult]) -> bool:
        return all(r.passed for r in results)
