from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from maais.domain.enums import QualityStatus
from maais.domain.json import JsonValue, content_hash, freeze_json
from maais.monitoring.schemas import ComponentName


class AdmissionCheck(StrEnum):
    KILL_SWITCH = "kill_switch"
    HEALTH = "health"
    BASELINE_WARMUP = "baseline_warmup"
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    BENCHMARK = "benchmark"


@dataclass(frozen=True, slots=True)
class OfficialAdmissionPolicy:
    mandatory_components: tuple[str, ...]
    health_max_age: timedelta
    market_observation_max_age: timedelta
    benchmark_max_age: timedelta
    minimum_baseline_samples: int
    volatility_multiplier: Decimal
    maximum_spread_fraction: Decimal

    def __post_init__(self) -> None:
        if (
            not self.mandatory_components
            or len(set(self.mandatory_components)) != len(self.mandatory_components)
            or any(not item for item in self.mandatory_components)
        ):
            raise ValueError("mandatory monitoring components must be nonempty and unique")
        for name in ("health_max_age", "market_observation_max_age", "benchmark_max_age"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.minimum_baseline_samples <= 1:
            raise ValueError("baseline warmup must require multiple samples")
        for name in ("volatility_multiplier", "maximum_spread_fraction"):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")

    @classmethod
    def conservative(cls) -> OfficialAdmissionPolicy:
        return cls(
            mandatory_components=tuple(item.value for item in ComponentName),
            health_max_age=timedelta(seconds=10),
            market_observation_max_age=timedelta(seconds=2),
            benchmark_max_age=timedelta(seconds=60),
            minimum_baseline_samples=60,
            volatility_multiplier=Decimal("1.5"),
            maximum_spread_fraction=Decimal("0.05"),
        )


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


@dataclass(frozen=True, slots=True)
class HealthObservation:
    component: str
    healthy: bool
    observed_at: datetime
    error: str | None

    def __post_init__(self) -> None:
        if not self.component:
            raise ValueError("health component is required")
        _require_utc(self.observed_at, "health observed_at")
        if self.healthy and self.error is not None:
            raise ValueError("healthy component cannot carry an error")


@dataclass(frozen=True, slots=True)
class RollingVolatilityBaseline:
    symbol: str
    timeframe: str
    sample_count: int
    baseline_std: Decimal
    current_std: Decimal
    spread_fraction: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.timeframe:
            raise ValueError("volatility baseline identity is invalid")
        if self.sample_count < 0:
            raise ValueError("volatility baseline sample count cannot be negative")
        if not self.baseline_std.is_finite() or self.baseline_std <= 0:
            raise ValueError("baseline std must be positive and finite")
        for name in ("current_std", "spread_fraction"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        _require_utc(self.observed_at, "volatility observed_at")


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    symbol: str
    return_fraction: Decimal
    observed_at: datetime
    source_event_id: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.source_event_id:
            raise ValueError("benchmark identity is invalid")
        if not self.return_fraction.is_finite():
            raise ValueError("benchmark return must be finite")
        _require_utc(self.observed_at, "benchmark observed_at")


@dataclass(frozen=True, slots=True)
class MonitoringAdmissionContext:
    symbol: str
    timeframe: str
    evaluated_at: datetime
    kill_switch_active: bool
    kill_switch_reason: str | None
    health: tuple[HealthObservation, ...]
    volatility: RollingVolatilityBaseline | None
    benchmark: BenchmarkObservation | None

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.timeframe:
            raise ValueError("monitoring context identity is invalid")
        _require_utc(self.evaluated_at, "monitoring evaluated_at")
        if self.kill_switch_active != (self.kill_switch_reason is not None):
            raise ValueError("kill switch state and reason must appear together")
        names = tuple(item.component for item in self.health)
        if len(set(names)) != len(names):
            raise ValueError("health observations must be unique by component")
        object.__setattr__(
            self,
            "health",
            tuple(sorted(self.health, key=lambda item: item.component)),
        )


@dataclass(frozen=True, slots=True)
class AdmissionGateResult:
    check: AdmissionCheck
    status: QualityStatus
    reason_code: str
    details: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("admission reason code is required")
        normalized = freeze_json(self.details)
        if not isinstance(normalized, Mapping):
            raise TypeError("admission gate details must be an object")
        object.__setattr__(self, "details", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "status": self.status,
            "reason_code": self.reason_code,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class MonitoringAdmissionDecision:
    allowed: bool
    input_snapshot: Mapping[str, JsonValue]
    gates: tuple[AdmissionGateResult, ...]
    blocking_checks: tuple[AdmissionCheck, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if tuple(item.check for item in self.gates) != tuple(AdmissionCheck):
            raise ValueError("monitoring decision must contain every ordered admission check")
        expected = tuple(
            item.check for item in self.gates if item.status is not QualityStatus.PASSED
        )
        if expected != self.blocking_checks or self.allowed != (not expected):
            raise ValueError("monitoring admission does not match its gate results")
        if len(self.content_hash) != 64:
            raise ValueError("monitoring admission content hash must be SHA-256")
        normalized = freeze_json(self.input_snapshot)
        if not isinstance(normalized, Mapping):
            raise TypeError("monitoring input snapshot must be an object")
        object.__setattr__(self, "input_snapshot", normalized)


def _gate(
    check: AdmissionCheck,
    status: QualityStatus,
    reason_code: str,
    **details: object,
) -> AdmissionGateResult:
    normalized = freeze_json(details)
    if not isinstance(normalized, Mapping):
        raise TypeError("admission gate details must be an object")
    return AdmissionGateResult(check, status, reason_code, normalized)


class OfficialMonitoringAdmission:
    def __init__(self, policy: OfficialAdmissionPolicy) -> None:
        self._policy = policy

    def evaluate(self, context: MonitoringAdmissionContext) -> MonitoringAdmissionDecision:
        input_snapshot = self._input_snapshot(context)
        gates = (
            self._kill_switch(context),
            self._health(context),
            self._baseline(context),
            self._volatility(context),
            self._liquidity(context),
            self._benchmark(context),
        )
        blocking = tuple(item.check for item in gates if item.status is not QualityStatus.PASSED)
        allowed = not blocking
        normalized = {
            "allowed": allowed,
            "input_snapshot": input_snapshot,
            "gates": [item.to_dict() for item in gates],
            "blocking_checks": blocking,
        }
        return MonitoringAdmissionDecision(
            allowed=allowed,
            input_snapshot=input_snapshot,
            gates=gates,
            blocking_checks=blocking,
            content_hash=content_hash(normalized),
        )

    def _input_snapshot(self, context: MonitoringAdmissionContext) -> Mapping[str, JsonValue]:
        volatility = context.volatility
        benchmark = context.benchmark
        normalized = freeze_json(
            {
                "context": {
                    "symbol": context.symbol,
                    "timeframe": context.timeframe,
                    "evaluated_at": context.evaluated_at,
                    "kill_switch_active": context.kill_switch_active,
                    "kill_switch_reason": context.kill_switch_reason,
                    "health": [
                        {
                            "component": item.component,
                            "healthy": item.healthy,
                            "observed_at": item.observed_at,
                            "error": item.error,
                        }
                        for item in context.health
                    ],
                    "volatility": (
                        {
                            "symbol": volatility.symbol,
                            "timeframe": volatility.timeframe,
                            "sample_count": volatility.sample_count,
                            "baseline_std": volatility.baseline_std,
                            "current_std": volatility.current_std,
                            "spread_fraction": volatility.spread_fraction,
                            "observed_at": volatility.observed_at,
                        }
                        if volatility is not None
                        else None
                    ),
                    "benchmark": (
                        {
                            "symbol": benchmark.symbol,
                            "return_fraction": benchmark.return_fraction,
                            "observed_at": benchmark.observed_at,
                            "source_event_id": benchmark.source_event_id,
                        }
                        if benchmark is not None
                        else None
                    ),
                },
                "policy": {
                    "mandatory_components": self._policy.mandatory_components,
                    "health_max_age_seconds": int(self._policy.health_max_age.total_seconds()),
                    "market_observation_max_age_seconds": int(
                        self._policy.market_observation_max_age.total_seconds()
                    ),
                    "benchmark_max_age_seconds": int(
                        self._policy.benchmark_max_age.total_seconds()
                    ),
                    "minimum_baseline_samples": self._policy.minimum_baseline_samples,
                    "volatility_multiplier": self._policy.volatility_multiplier,
                    "maximum_spread_fraction": self._policy.maximum_spread_fraction,
                },
            }
        )
        if not isinstance(normalized, Mapping):
            raise TypeError("monitoring input snapshot must be an object")
        return normalized

    @staticmethod
    def _kill_switch(context: MonitoringAdmissionContext) -> AdmissionGateResult:
        if context.kill_switch_active:
            return _gate(
                AdmissionCheck.KILL_SWITCH,
                QualityStatus.FAILED,
                "kill_switch_active",
                reason=context.kill_switch_reason,
            )
        return _gate(
            AdmissionCheck.KILL_SWITCH,
            QualityStatus.PASSED,
            "kill_switch_clear",
        )

    def _health(self, context: MonitoringAdmissionContext) -> AdmissionGateResult:
        observations = {item.component: item for item in context.health}
        missing = sorted(set(self._policy.mandatory_components) - observations.keys())
        if missing:
            return _gate(
                AdmissionCheck.HEALTH,
                QualityStatus.NOT_APPLICABLE,
                "mandatory_health_missing",
                components=missing,
            )
        unhealthy = sorted(
            component
            for component in self._policy.mandatory_components
            if not observations[component].healthy
        )
        if unhealthy:
            return _gate(
                AdmissionCheck.HEALTH,
                QualityStatus.FAILED,
                "mandatory_health_unhealthy",
                components=unhealthy,
            )
        future = sorted(
            component
            for component in self._policy.mandatory_components
            if observations[component].observed_at > context.evaluated_at
        )
        if future:
            return _gate(
                AdmissionCheck.HEALTH,
                QualityStatus.FAILED,
                "mandatory_health_from_future",
                components=future,
            )
        stale = sorted(
            component
            for component in self._policy.mandatory_components
            if context.evaluated_at - observations[component].observed_at
            > self._policy.health_max_age
        )
        if stale:
            return _gate(
                AdmissionCheck.HEALTH,
                QualityStatus.FAILED,
                "mandatory_health_stale",
                components=stale,
                maximum_age_seconds=int(self._policy.health_max_age.total_seconds()),
            )
        return _gate(
            AdmissionCheck.HEALTH,
            QualityStatus.PASSED,
            "mandatory_health_fresh",
            components=self._policy.mandatory_components,
        )

    def _baseline(self, context: MonitoringAdmissionContext) -> AdmissionGateResult:
        baseline = context.volatility
        if baseline is None:
            return _gate(
                AdmissionCheck.BASELINE_WARMUP,
                QualityStatus.NOT_APPLICABLE,
                "baseline_missing",
            )
        if (baseline.symbol, baseline.timeframe) != (context.symbol, context.timeframe):
            return _gate(
                AdmissionCheck.BASELINE_WARMUP,
                QualityStatus.FAILED,
                "baseline_identity_mismatch",
                expected_symbol=context.symbol,
                expected_timeframe=context.timeframe,
                actual_symbol=baseline.symbol,
                actual_timeframe=baseline.timeframe,
            )
        if baseline.observed_at > context.evaluated_at:
            return _gate(
                AdmissionCheck.BASELINE_WARMUP,
                QualityStatus.FAILED,
                "baseline_from_future",
            )
        if context.evaluated_at - baseline.observed_at > self._policy.market_observation_max_age:
            return _gate(
                AdmissionCheck.BASELINE_WARMUP,
                QualityStatus.FAILED,
                "baseline_observation_stale",
            )
        if baseline.sample_count < self._policy.minimum_baseline_samples:
            return _gate(
                AdmissionCheck.BASELINE_WARMUP,
                QualityStatus.NOT_APPLICABLE,
                "baseline_cold",
                sample_count=baseline.sample_count,
                minimum_samples=self._policy.minimum_baseline_samples,
            )
        return _gate(
            AdmissionCheck.BASELINE_WARMUP,
            QualityStatus.PASSED,
            "baseline_warm",
            sample_count=baseline.sample_count,
            baseline_std=baseline.baseline_std,
        )

    def _volatility(self, context: MonitoringAdmissionContext) -> AdmissionGateResult:
        baseline_gate = self._baseline(context)
        baseline = context.volatility
        if baseline_gate.status is not QualityStatus.PASSED or baseline is None:
            return _gate(
                AdmissionCheck.VOLATILITY,
                QualityStatus.NOT_APPLICABLE,
                "baseline_unavailable",
            )
        threshold = baseline.baseline_std * self._policy.volatility_multiplier
        if baseline.current_std > threshold:
            return _gate(
                AdmissionCheck.VOLATILITY,
                QualityStatus.FAILED,
                "volatility_circuit_breaker",
                current_std=baseline.current_std,
                baseline_std=baseline.baseline_std,
                threshold=threshold,
            )
        return _gate(
            AdmissionCheck.VOLATILITY,
            QualityStatus.PASSED,
            "volatility_within_limit",
            current_std=baseline.current_std,
            threshold=threshold,
        )

    def _liquidity(self, context: MonitoringAdmissionContext) -> AdmissionGateResult:
        baseline = context.volatility
        if self._baseline(context).status is not QualityStatus.PASSED or baseline is None:
            return _gate(
                AdmissionCheck.LIQUIDITY,
                QualityStatus.NOT_APPLICABLE,
                "liquidity_observation_missing",
            )
        if baseline.spread_fraction > self._policy.maximum_spread_fraction:
            return _gate(
                AdmissionCheck.LIQUIDITY,
                QualityStatus.FAILED,
                "liquidity_spread_exceeded",
                spread_fraction=baseline.spread_fraction,
                maximum_spread_fraction=self._policy.maximum_spread_fraction,
            )
        return _gate(
            AdmissionCheck.LIQUIDITY,
            QualityStatus.PASSED,
            "liquidity_within_limit",
            spread_fraction=baseline.spread_fraction,
        )

    def _benchmark(self, context: MonitoringAdmissionContext) -> AdmissionGateResult:
        benchmark = context.benchmark
        if benchmark is None:
            return _gate(
                AdmissionCheck.BENCHMARK,
                QualityStatus.NOT_APPLICABLE,
                "benchmark_missing",
            )
        if benchmark.observed_at > context.evaluated_at:
            return _gate(
                AdmissionCheck.BENCHMARK,
                QualityStatus.FAILED,
                "benchmark_from_future",
                source_event_id=benchmark.source_event_id,
            )
        if context.evaluated_at - benchmark.observed_at > self._policy.benchmark_max_age:
            return _gate(
                AdmissionCheck.BENCHMARK,
                QualityStatus.FAILED,
                "benchmark_stale",
                source_event_id=benchmark.source_event_id,
            )
        return _gate(
            AdmissionCheck.BENCHMARK,
            QualityStatus.PASSED,
            "benchmark_explicit_and_fresh",
            benchmark_symbol=benchmark.symbol,
            return_fraction=benchmark.return_fraction,
            source_event_id=benchmark.source_event_id,
            observed_at=benchmark.observed_at,
        )
