from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from maais.domain.enums import QualityStatus
from maais.domain.json import JsonValue, content_hash, freeze_json
from maais.market_data.frames import CausalMinuteFrame


class IntegrityCheck(StrEnum):
    REQUIRED_SOURCES = "required_sources"
    MISSING_INTERVAL = "missing_interval"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    SEQUENCE = "sequence"
    VENUE_TIMESTAMP = "venue_timestamp"
    OBSERVED_LAG = "observed_lag"
    SOURCE_FRESHNESS = "source_freshness"
    CLOCK_DRIFT = "clock_drift"
    STALE_BOOK = "stale_book"
    API_OUTAGE = "api_outage"
    OHLC = "ohlc"
    VOLUME = "volume"
    CROSSED_BOOK = "crossed_book"
    SYMBOL_STATE = "symbol_state"
    HISTORICAL_COVERAGE = "historical_coverage"
    CLOSE_RETURN_OUTLIER = "close_return_outlier"
    FUTURES_SPOT_BASIS = "futures_spot_basis"
    SECONDARY_REFERENCE = "secondary_reference"


class FrameAdmission(StrEnum):
    ADMITTED = "admitted"
    QUARANTINED = "quarantined"


class SequenceRule(StrEnum):
    CONTIGUOUS = "contiguous"
    MONOTONIC = "monotonic"


@dataclass(frozen=True, slots=True)
class IntegrityPolicy:
    required_checks: frozenset[IntegrityCheck]
    required_sources: frozenset[str]
    sequence_rules: Mapping[str, SequenceRule]
    source_max_age: Mapping[str, timedelta]
    max_venue_timestamp_skew: timedelta
    venue_timestamp_skew_overrides: Mapping[str, timedelta]
    max_venue_clock_drift: timedelta
    max_book_age: timedelta
    max_decision_lag: timedelta
    minimum_history_bars: int
    minimum_outlier_returns: int
    outlier_z_threshold: Decimal
    maximum_basis_fraction: Decimal
    maximum_secondary_divergence: Decimal

    def __post_init__(self) -> None:
        if not self.required_checks or not self.required_sources:
            raise ValueError("official integrity policy cannot be empty")
        for value, field in (
            (self.max_venue_timestamp_skew, "max_venue_timestamp_skew"),
            (self.max_venue_clock_drift, "max_venue_clock_drift"),
            (self.max_book_age, "max_book_age"),
            (self.max_decision_lag, "max_decision_lag"),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{field} must be positive")
        if self.minimum_history_bars <= 0 or self.minimum_outlier_returns < 2:
            raise ValueError("integrity warm-up windows are invalid")
        for value, field in (
            (self.outlier_z_threshold, "outlier_z_threshold"),
            (self.maximum_basis_fraction, "maximum_basis_fraction"),
            (self.maximum_secondary_divergence, "maximum_secondary_divergence"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{field} must be a positive finite Decimal")
        object.__setattr__(self, "source_max_age", MappingProxyType(dict(self.source_max_age)))
        if not set(self.venue_timestamp_skew_overrides).issubset(self.required_sources):
            raise ValueError("venue timestamp skew overrides must name required sources")
        if any(value <= timedelta(0) for value in self.venue_timestamp_skew_overrides.values()):
            raise ValueError("venue timestamp skew overrides must be positive")
        object.__setattr__(
            self,
            "venue_timestamp_skew_overrides",
            MappingProxyType(dict(self.venue_timestamp_skew_overrides)),
        )
        if not self.sequence_rules or not set(self.sequence_rules).issubset(self.required_sources):
            raise ValueError("sequence rules must name required sources")
        object.__setattr__(self, "sequence_rules", MappingProxyType(dict(self.sequence_rules)))

    @classmethod
    def official(cls) -> IntegrityPolicy:
        return cls(
            required_checks=frozenset(IntegrityCheck),
            required_sources=frozenset(
                {
                    "closed_bar",
                    "order_book",
                    "mark_funding",
                    "primary_spot",
                    "secondary_venue",
                    "venue_clock",
                    "symbol_state",
                }
            ),
            sequence_rules={
                "closed_bar": SequenceRule.CONTIGUOUS,
                "order_book": SequenceRule.MONOTONIC,
            },
            source_max_age={
                "closed_bar": timedelta(seconds=1),
                "order_book": timedelta(seconds=2),
                "mark_funding": timedelta(seconds=2),
                "primary_spot": timedelta(seconds=5),
                "secondary_venue": timedelta(seconds=5),
                "venue_clock": timedelta(seconds=60),
                "symbol_state": timedelta(hours=24),
            },
            max_venue_timestamp_skew=timedelta(seconds=1),
            venue_timestamp_skew_overrides={
                "primary_spot": timedelta(seconds=5),
                "secondary_venue": timedelta(seconds=2),
            },
            max_venue_clock_drift=timedelta(seconds=1),
            max_book_age=timedelta(seconds=2),
            max_decision_lag=timedelta(seconds=5),
            minimum_history_bars=60,
            minimum_outlier_returns=20,
            outlier_z_threshold=Decimal("5"),
            maximum_basis_fraction=Decimal("0.02"),
            maximum_secondary_divergence=Decimal("0.02"),
        )


@dataclass(frozen=True, slots=True)
class IntegrityContext:
    frame: CausalMinuteFrame
    evaluated_at: datetime
    previous_bar_close_at: datetime | None
    previous_close: Decimal | None
    prior_sequences: Mapping[str, int]
    recent_close_returns: tuple[Decimal, ...]
    historical_bar_count: int

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() != timedelta(0):
            raise ValueError("evaluated_at must be UTC-aware")
        if self.previous_bar_close_at is not None and (
            self.previous_bar_close_at.tzinfo is None
            or self.previous_bar_close_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("previous_bar_close_at must be UTC-aware")
        if self.previous_close is not None and (
            not isinstance(self.previous_close, Decimal)
            or not self.previous_close.is_finite()
            or self.previous_close <= 0
        ):
            raise ValueError("previous_close must be a positive finite Decimal")
        if self.historical_bar_count < 0:
            raise ValueError("historical_bar_count must be nonnegative")
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in self.recent_close_returns
        ):
            raise ValueError("recent close returns must be finite Decimals")
        if any(not name or value < 0 for name, value in self.prior_sequences.items()):
            raise ValueError("prior source sequences must be named and nonnegative")
        object.__setattr__(self, "prior_sequences", MappingProxyType(dict(self.prior_sequences)))


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    check: IntegrityCheck
    status: QualityStatus
    reason_code: str
    details: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("integrity reason_code is required")
        normalized = freeze_json(self.details)
        if not isinstance(normalized, Mapping):
            raise TypeError("integrity details must be an object")
        object.__setattr__(self, "details", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "status": self.status,
            "reason_code": self.reason_code,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class IntegrityAssessment:
    frame_id: UUID
    admission: FrameAdmission
    quality_status: QualityStatus
    results: tuple[IntegrityResult, ...]
    blocking_checks: tuple[IntegrityCheck, ...]
    content_hash: str

    @property
    def is_expected_warmup(self) -> bool:
        """True only when every blocker is an ordinary causal-history warm-up gap."""

        expected_reasons = {
            IntegrityCheck.MISSING_INTERVAL: frozenset({"previous_bar_missing"}),
            IntegrityCheck.HISTORICAL_COVERAGE: frozenset({"historical_warmup_incomplete"}),
            IntegrityCheck.CLOSE_RETURN_OUTLIER: frozenset(
                {"previous_close_missing", "return_warmup_incomplete"}
            ),
        }
        if not self.blocking_checks:
            return False
        by_check = {result.check: result for result in self.results}
        for check in self.blocking_checks:
            result = by_check.get(check)
            if (
                result is None
                or result.status is not QualityStatus.NOT_APPLICABLE
                or result.reason_code not in expected_reasons.get(check, frozenset())
            ):
                return False
        return True


def _result(
    check: IntegrityCheck,
    status: QualityStatus,
    reason_code: str,
    **details: object,
) -> IntegrityResult:
    normalized = freeze_json(details)
    assert isinstance(normalized, Mapping)
    return IntegrityResult(check, status, reason_code, normalized)


class MarketIntegrityStateMachine:
    def __init__(self, policy: IntegrityPolicy) -> None:
        self._policy = policy

    def evaluate(self, context: IntegrityContext) -> IntegrityAssessment:
        results = (
            self._required_sources(context),
            self._missing_interval(context),
            _result(
                IntegrityCheck.DUPLICATE_CONFLICT,
                QualityStatus.PASSED,
                "frame_identity_canonical",
            ),
            self._sequence(context),
            self._venue_timestamp(context),
            self._observed_lag(context),
            self._source_freshness(context),
            self._clock_drift(context),
            self._stale_book(context),
            self._api_outage(context),
            self._ohlc(context),
            self._volume(context),
            self._crossed_book(context),
            self._symbol_state(context),
            self._historical_coverage(context),
            self._close_return_outlier(context),
            self._futures_spot_basis(context),
            self._secondary_reference(context),
        )
        blocking = tuple(
            result.check
            for result in results
            if result.status is QualityStatus.FAILED
            or (
                result.status is QualityStatus.NOT_APPLICABLE
                and result.check in self._policy.required_checks
            )
        )
        admission = FrameAdmission.QUARANTINED if blocking else FrameAdmission.ADMITTED
        quality = QualityStatus.FAILED if blocking else QualityStatus.PASSED
        normalized = {
            "frame_id": context.frame.frame_id,
            "admission": admission,
            "quality_status": quality,
            "results": [item.to_dict() for item in results],
            "blocking_checks": blocking,
        }
        return IntegrityAssessment(
            frame_id=context.frame.frame_id,
            admission=admission,
            quality_status=quality,
            results=results,
            blocking_checks=blocking,
            content_hash=content_hash(normalized),
        )

    def _required_sources(self, context: IntegrityContext) -> IntegrityResult:
        missing = sorted(self._policy.required_sources - context.frame.source_manifest.keys())
        if missing:
            return _result(
                IntegrityCheck.REQUIRED_SOURCES,
                QualityStatus.NOT_APPLICABLE,
                "required_sources_missing",
                missing=missing,
            )
        return _result(
            IntegrityCheck.REQUIRED_SOURCES,
            QualityStatus.PASSED,
            "required_sources_present",
        )

    @staticmethod
    def _missing_interval(context: IntegrityContext) -> IntegrityResult:
        if context.previous_bar_close_at is None:
            return _result(
                IntegrityCheck.MISSING_INTERVAL,
                QualityStatus.NOT_APPLICABLE,
                "previous_bar_missing",
            )
        if context.previous_bar_close_at != context.frame.bar.bar_open_at:
            return _result(
                IntegrityCheck.MISSING_INTERVAL,
                QualityStatus.FAILED,
                "bar_interval_gap",
                expected_open=context.previous_bar_close_at,
                actual_open=context.frame.bar.bar_open_at,
            )
        return _result(
            IntegrityCheck.MISSING_INTERVAL,
            QualityStatus.PASSED,
            "bar_interval_contiguous",
        )

    def _sequence(self, context: IntegrityContext) -> IntegrityResult:
        missing: list[str] = []
        bad: list[dict[str, object]] = []
        for name, rule in sorted(self._policy.sequence_rules.items()):
            source = context.frame.source_manifest.get(name)
            if source is None or source.sequence is None:
                missing.append(name)
                continue
            previous = context.prior_sequences.get(name)
            if previous is None:
                continue
            contiguous_violation = (
                rule is SequenceRule.CONTIGUOUS and source.sequence != previous + 1
            )
            monotonic_violation = rule is SequenceRule.MONOTONIC and source.sequence <= previous
            if contiguous_violation or monotonic_violation:
                bad.append(
                    {
                        "source": name,
                        "rule": rule,
                        "previous": previous,
                        "current": source.sequence,
                    }
                )
        if missing:
            return _result(
                IntegrityCheck.SEQUENCE,
                QualityStatus.NOT_APPLICABLE,
                "required_sequence_missing",
                sources=missing,
            )
        if bad:
            return _result(
                IntegrityCheck.SEQUENCE,
                QualityStatus.FAILED,
                "sequence_gap_or_regression",
                sources=bad,
            )
        return _result(
            IntegrityCheck.SEQUENCE,
            QualityStatus.PASSED,
            "sequences_contiguous_or_initialized",
        )

    def _venue_timestamp(self, context: IntegrityContext) -> IntegrityResult:
        bad: list[str] = []
        observations: dict[str, dict[str, Decimal]] = {}
        for name, source in sorted(context.frame.source_manifest.items()):
            limit = self._policy.venue_timestamp_skew_overrides.get(
                name,
                self._policy.max_venue_timestamp_skew,
            )
            skew = abs(source.observed_at - source.venue_event_at)
            observations[name] = {
                "skew_seconds": Decimal(str(skew.total_seconds())),
                "limit_seconds": Decimal(str(limit.total_seconds())),
            }
            if skew > limit:
                bad.append(name)
        if bad:
            return _result(
                IntegrityCheck.VENUE_TIMESTAMP,
                QualityStatus.FAILED,
                "venue_timestamp_skew",
                sources=bad,
                observations=observations,
            )
        return _result(
            IntegrityCheck.VENUE_TIMESTAMP,
            QualityStatus.PASSED,
            "venue_timestamps_within_skew",
            observations=observations,
        )

    def _observed_lag(self, context: IntegrityContext) -> IntegrityResult:
        lag = context.evaluated_at - context.frame.cutoff_at
        if lag < timedelta(0):
            return _result(
                IntegrityCheck.OBSERVED_LAG,
                QualityStatus.FAILED,
                "evaluation_precedes_frame",
            )
        if lag > self._policy.max_decision_lag:
            return _result(
                IntegrityCheck.OBSERVED_LAG,
                QualityStatus.FAILED,
                "decision_lag_exceeded",
                lag_seconds=str(Decimal(str(lag.total_seconds()))),
            )
        return _result(
            IntegrityCheck.OBSERVED_LAG,
            QualityStatus.PASSED,
            "decision_lag_within_limit",
        )

    def _source_freshness(self, context: IntegrityContext) -> IntegrityResult:
        missing_policy: list[str] = []
        stale: list[str] = []
        for name in sorted(self._policy.required_sources):
            source = context.frame.source_manifest.get(name)
            maximum_age = self._policy.source_max_age.get(name)
            if maximum_age is None:
                missing_policy.append(name)
            elif source is not None and context.frame.cutoff_at - source.observed_at > maximum_age:
                stale.append(name)
        if missing_policy:
            return _result(
                IntegrityCheck.SOURCE_FRESHNESS,
                QualityStatus.NOT_APPLICABLE,
                "source_freshness_policy_missing",
                sources=missing_policy,
            )
        if stale:
            return _result(
                IntegrityCheck.SOURCE_FRESHNESS,
                QualityStatus.FAILED,
                "source_stale",
                sources=stale,
            )
        return _result(
            IntegrityCheck.SOURCE_FRESHNESS,
            QualityStatus.PASSED,
            "sources_fresh",
        )

    def _clock_drift(self, context: IntegrityContext) -> IntegrityResult:
        source = context.frame.source_manifest.get("venue_clock")
        if source is None or context.frame.venue_server_time is None:
            return _result(
                IntegrityCheck.CLOCK_DRIFT,
                QualityStatus.NOT_APPLICABLE,
                "venue_clock_missing",
            )
        drift = abs(context.frame.venue_server_time - source.observed_at)
        if drift > self._policy.max_venue_clock_drift:
            return _result(
                IntegrityCheck.CLOCK_DRIFT,
                QualityStatus.FAILED,
                "venue_clock_drift_exceeded",
                drift_seconds=str(Decimal(str(drift.total_seconds()))),
            )
        return _result(
            IntegrityCheck.CLOCK_DRIFT,
            QualityStatus.PASSED,
            "venue_clock_within_limit",
        )

    def _stale_book(self, context: IntegrityContext) -> IntegrityResult:
        source = context.frame.source_manifest.get("order_book")
        if source is None:
            return _result(
                IntegrityCheck.STALE_BOOK,
                QualityStatus.NOT_APPLICABLE,
                "order_book_missing",
            )
        if context.frame.cutoff_at - source.observed_at > self._policy.max_book_age:
            return _result(
                IntegrityCheck.STALE_BOOK,
                QualityStatus.FAILED,
                "order_book_stale",
            )
        return _result(
            IntegrityCheck.STALE_BOOK,
            QualityStatus.PASSED,
            "order_book_fresh",
        )

    def _api_outage(self, context: IntegrityContext) -> IntegrityResult:
        age = context.evaluated_at - context.frame.cutoff_at
        if age > self._policy.max_decision_lag:
            return _result(
                IntegrityCheck.API_OUTAGE,
                QualityStatus.FAILED,
                "market_data_outage",
            )
        return _result(
            IntegrityCheck.API_OUTAGE,
            QualityStatus.PASSED,
            "market_data_current",
        )

    @staticmethod
    def _ohlc(context: IntegrityContext) -> IntegrityResult:
        bar = context.frame.bar
        valid = bar.low <= min(bar.open, bar.close) and bar.high >= max(bar.open, bar.close)
        return _result(
            IntegrityCheck.OHLC,
            QualityStatus.PASSED if valid else QualityStatus.FAILED,
            "ohlc_valid" if valid else "ohlc_invalid",
        )

    @staticmethod
    def _volume(context: IntegrityContext) -> IntegrityResult:
        valid = context.frame.bar.volume >= 0 and context.frame.bar.quote_volume >= 0
        return _result(
            IntegrityCheck.VOLUME,
            QualityStatus.PASSED if valid else QualityStatus.FAILED,
            "volume_nonnegative" if valid else "volume_negative",
        )

    @staticmethod
    def _crossed_book(context: IntegrityContext) -> IntegrityResult:
        if context.frame.best_bid is None or context.frame.best_ask is None:
            return _result(
                IntegrityCheck.CROSSED_BOOK,
                QualityStatus.NOT_APPLICABLE,
                "order_book_missing",
            )
        valid = context.frame.best_bid < context.frame.best_ask
        return _result(
            IntegrityCheck.CROSSED_BOOK,
            QualityStatus.PASSED if valid else QualityStatus.FAILED,
            "book_not_crossed" if valid else "book_crossed_or_locked",
        )

    @staticmethod
    def _symbol_state(context: IntegrityContext) -> IntegrityResult:
        if context.frame.symbol_status is None:
            return _result(
                IntegrityCheck.SYMBOL_STATE,
                QualityStatus.NOT_APPLICABLE,
                "symbol_state_missing",
            )
        valid = context.frame.symbol_status == "TRADING"
        return _result(
            IntegrityCheck.SYMBOL_STATE,
            QualityStatus.PASSED if valid else QualityStatus.FAILED,
            "symbol_trading" if valid else "symbol_not_trading",
            symbol_status=context.frame.symbol_status,
        )

    def _historical_coverage(self, context: IntegrityContext) -> IntegrityResult:
        if context.historical_bar_count < self._policy.minimum_history_bars:
            return _result(
                IntegrityCheck.HISTORICAL_COVERAGE,
                QualityStatus.NOT_APPLICABLE,
                "historical_warmup_incomplete",
                actual=context.historical_bar_count,
                required=self._policy.minimum_history_bars,
            )
        return _result(
            IntegrityCheck.HISTORICAL_COVERAGE,
            QualityStatus.PASSED,
            "historical_coverage_sufficient",
        )

    def _close_return_outlier(self, context: IntegrityContext) -> IntegrityResult:
        if context.previous_close is None:
            return _result(
                IntegrityCheck.CLOSE_RETURN_OUTLIER,
                QualityStatus.NOT_APPLICABLE,
                "previous_close_missing",
            )
        values = context.recent_close_returns
        if len(values) < self._policy.minimum_outlier_returns:
            return _result(
                IntegrityCheck.CLOSE_RETURN_OUTLIER,
                QualityStatus.NOT_APPLICABLE,
                "return_warmup_incomplete",
                actual=len(values),
                required=self._policy.minimum_outlier_returns,
            )
        current = (context.frame.bar.close - context.previous_close) / context.previous_close
        mean = sum(values, start=Decimal("0")) / Decimal(len(values))
        variance = sum(((value - mean) ** 2 for value in values), start=Decimal("0")) / Decimal(
            len(values) - 1
        )
        if variance == 0:
            failed = current != mean
            return _result(
                IntegrityCheck.CLOSE_RETURN_OUTLIER,
                QualityStatus.FAILED if failed else QualityStatus.PASSED,
                "zero_variance_outlier" if failed else "return_matches_zero_variance_baseline",
                current_return=current,
                baseline_mean=mean,
            )
        z_score = abs(current - mean) / variance.sqrt()
        failed = z_score > self._policy.outlier_z_threshold
        return _result(
            IntegrityCheck.CLOSE_RETURN_OUTLIER,
            QualityStatus.FAILED if failed else QualityStatus.PASSED,
            "return_outlier" if failed else "return_within_baseline",
            current_return=current,
            z_score=z_score,
        )

    def _futures_spot_basis(self, context: IntegrityContext) -> IntegrityResult:
        mark = context.frame.mark_price
        spot = context.frame.primary_spot_price
        if mark is None or spot is None:
            return _result(
                IntegrityCheck.FUTURES_SPOT_BASIS,
                QualityStatus.NOT_APPLICABLE,
                "futures_or_spot_price_missing",
            )
        divergence = abs(mark - spot) / spot
        failed = divergence > self._policy.maximum_basis_fraction
        return _result(
            IntegrityCheck.FUTURES_SPOT_BASIS,
            QualityStatus.FAILED if failed else QualityStatus.PASSED,
            "basis_divergence_exceeded" if failed else "basis_within_limit",
            divergence=divergence,
            futures_mark=mark,
            primary_spot=spot,
        )

    def _secondary_reference(self, context: IntegrityContext) -> IntegrityResult:
        mark = context.frame.mark_price
        reference = context.frame.secondary_venue_price
        if reference is None:
            return _result(
                IntegrityCheck.SECONDARY_REFERENCE,
                QualityStatus.NOT_APPLICABLE,
                "secondary_reference_missing",
            )
        if mark is None:
            return _result(
                IntegrityCheck.SECONDARY_REFERENCE,
                QualityStatus.NOT_APPLICABLE,
                "futures_mark_missing",
            )
        divergence = abs(mark - reference) / reference
        failed = divergence > self._policy.maximum_secondary_divergence
        return _result(
            IntegrityCheck.SECONDARY_REFERENCE,
            QualityStatus.FAILED if failed else QualityStatus.PASSED,
            ("secondary_divergence_exceeded" if failed else "secondary_divergence_within_limit"),
            divergence=divergence,
            futures_mark=mark,
            secondary_reference=reference,
        )
