from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from maais.domain.enums import QualityStatus
from maais.monitoring.admission import (
    AdmissionCheck,
    BenchmarkObservation,
    HealthObservation,
    MonitoringAdmissionContext,
    OfficialAdmissionPolicy,
    OfficialMonitoringAdmission,
    RollingVolatilityBaseline,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _health(policy: OfficialAdmissionPolicy) -> tuple[HealthObservation, ...]:
    return tuple(
        HealthObservation(component, True, NOW - timedelta(milliseconds=100), None)
        for component in policy.mandatory_components
    )


def _context(**changes) -> MonitoringAdmissionContext:
    policy = OfficialAdmissionPolicy.conservative()
    values = {
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "evaluated_at": NOW,
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "health": _health(policy),
        "volatility": RollingVolatilityBaseline(
            symbol="BTCUSDT",
            timeframe="1m",
            sample_count=60,
            baseline_std=Decimal("0.002"),
            current_std=Decimal("0.0025"),
            spread_fraction=Decimal("0.001"),
            observed_at=NOW - timedelta(milliseconds=100),
        ),
        "benchmark": BenchmarkObservation(
            symbol="BTCUSDT",
            return_fraction=Decimal("0"),
            observed_at=NOW - timedelta(milliseconds=100),
            source_event_id="benchmark-1",
        ),
    }
    values.update(changes)
    return MonitoringAdmissionContext(**values)


def _gate(decision, check: AdmissionCheck):
    return next(item for item in decision.gates if item.check is check)


def test_complete_fresh_monitoring_context_admits_and_is_hash_stable() -> None:
    policy = OfficialAdmissionPolicy.conservative()
    engine = OfficialMonitoringAdmission(policy)
    first = engine.evaluate(_context())
    second = engine.evaluate(_context())

    assert first.allowed
    assert first.content_hash == second.content_hash
    assert {gate.check for gate in first.gates} == set(AdmissionCheck)
    assert all(gate.status is QualityStatus.PASSED for gate in first.gates)
    changed_health = tuple(
        replace(item, observed_at=item.observed_at - timedelta(milliseconds=1))
        for item in _context().health
    )
    changed = engine.evaluate(_context(health=changed_health))
    assert changed.allowed
    assert changed.content_hash != first.content_hash
    permuted = engine.evaluate(_context(health=tuple(reversed(_context().health))))
    assert permuted.content_hash == first.content_hash


def test_missing_or_stale_mandatory_health_fails_closed() -> None:
    policy = OfficialAdmissionPolicy.conservative()
    health = _health(policy)
    missing = health[1:]
    stale = (
        replace(health[0], observed_at=NOW - policy.health_max_age - timedelta(seconds=1)),
        *health[1:],
    )
    engine = OfficialMonitoringAdmission(policy)

    missing_result = engine.evaluate(_context(health=missing))
    stale_result = engine.evaluate(_context(health=stale))

    assert not missing_result.allowed
    assert _gate(missing_result, AdmissionCheck.HEALTH).reason_code == "mandatory_health_missing"
    assert not stale_result.allowed
    assert _gate(stale_result, AdmissionCheck.HEALTH).reason_code == "mandatory_health_stale"


def test_black_swan_baseline_must_be_warm_per_symbol_and_timeframe() -> None:
    cold = replace(_context().volatility, sample_count=59)
    wrong_identity = replace(_context().volatility, symbol="ETHUSDT")
    engine = OfficialMonitoringAdmission(OfficialAdmissionPolicy.conservative())

    cold_result = engine.evaluate(_context(volatility=cold))
    identity_result = engine.evaluate(_context(volatility=wrong_identity))

    assert not cold_result.allowed
    assert _gate(cold_result, AdmissionCheck.BASELINE_WARMUP).reason_code == "baseline_cold"
    assert not identity_result.allowed
    assert (
        _gate(identity_result, AdmissionCheck.BASELINE_WARMUP).reason_code
        == "baseline_identity_mismatch"
    )


def test_warm_extreme_volatility_and_spread_each_block() -> None:
    base = _context().volatility
    high_vol = replace(base, current_std=Decimal("0.004"))
    wide_spread = replace(base, spread_fraction=Decimal("0.06"))
    engine = OfficialMonitoringAdmission(OfficialAdmissionPolicy.conservative())

    vol_result = engine.evaluate(_context(volatility=high_vol))
    spread_result = engine.evaluate(_context(volatility=wide_spread))

    assert _gate(vol_result, AdmissionCheck.VOLATILITY).status is QualityStatus.FAILED
    assert _gate(spread_result, AdmissionCheck.LIQUIDITY).status is QualityStatus.FAILED


def test_benchmark_must_be_explicit_but_an_observed_zero_return_is_valid() -> None:
    engine = OfficialMonitoringAdmission(OfficialAdmissionPolicy.conservative())

    absent = engine.evaluate(_context(benchmark=None))
    explicit_zero = engine.evaluate(_context())

    assert not absent.allowed
    assert _gate(absent, AdmissionCheck.BENCHMARK).reason_code == "benchmark_missing"
    assert explicit_zero.allowed
    benchmark_gate = _gate(explicit_zero, AdmissionCheck.BENCHMARK)
    assert benchmark_gate.details["source_event_id"] == "benchmark-1"
