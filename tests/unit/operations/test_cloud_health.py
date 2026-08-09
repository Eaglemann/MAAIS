from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from maais.observability.audit import HealthSeverity, HealthStatus
from maais.operations.cloud_health import (
    CRITICAL_COMPONENTS,
    WARNING_COMPONENTS,
    CloudHealthComponent,
    evaluate_cloud_components,
)

NOW = datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)


def _healthy_components() -> dict[str, CloudHealthComponent]:
    return {
        name: CloudHealthComponent(
            passed=True,
            failure_severity=(
                HealthSeverity.CRITICAL if name in CRITICAL_COMPONENTS else HealthSeverity.WARNING
            ),
            reason_code="check_passed",
            evidence={"observed": True},
        )
        for name in (*sorted(CRITICAL_COMPONENTS), *sorted(WARNING_COMPONENTS))
    }


def test_cloud_health_requires_the_exact_component_contract() -> None:
    components = _healthy_components()
    components.pop("audit_chain")

    with pytest.raises(ValueError, match="component contract"):
        evaluate_cloud_components(components, checked_at=NOW)


@pytest.mark.parametrize("failed_name", sorted(CRITICAL_COMPONENTS))
def test_every_critical_component_fails_the_cloud_health_gate(failed_name: str) -> None:
    components = _healthy_components()
    components[failed_name] = CloudHealthComponent(
        passed=False,
        failure_severity=HealthSeverity.CRITICAL,
        reason_code=f"{failed_name}_failed",
        evidence={"observed": False},
    )

    assessment = evaluate_cloud_components(components, checked_at=NOW)

    assert assessment.overall_status is HealthStatus.CRITICAL
    assert assessment.severity is HealthSeverity.CRITICAL
    assert assessment.failed_check_names == (failed_name,)
    assert assessment.components[failed_name]["status"] == "failed"


@pytest.mark.parametrize("failed_name", sorted(WARNING_COMPONENTS))
def test_warning_component_degrades_without_hiding_healthy_critical_checks(
    failed_name: str,
) -> None:
    components = _healthy_components()
    components[failed_name] = CloudHealthComponent(
        passed=False,
        failure_severity=HealthSeverity.WARNING,
        reason_code=f"{failed_name}_failed",
        evidence={"observed": False},
    )

    assessment = evaluate_cloud_components(components, checked_at=NOW)

    assert assessment.overall_status is HealthStatus.WARNING
    assert assessment.severity is HealthSeverity.WARNING
    assert assessment.failed_check_names == (failed_name,)


def test_critical_status_dominates_warning_and_healthy_has_no_failed_checks() -> None:
    healthy = evaluate_cloud_components(_healthy_components(), checked_at=NOW)
    assert healthy.overall_status is HealthStatus.HEALTHY
    assert healthy.severity is HealthSeverity.INFO
    assert healthy.failed_check_names == ()

    components = _healthy_components()
    components["backup"] = CloudHealthComponent(
        passed=False,
        failure_severity=HealthSeverity.CRITICAL,
        reason_code="backup_stale",
        evidence={"age_seconds": 900},
    )
    components["sentry_delivery"] = CloudHealthComponent(
        passed=False,
        failure_severity=HealthSeverity.WARNING,
        reason_code="sentry_unconfirmed",
        evidence={"delivery_confirmed": False},
    )

    failed = evaluate_cloud_components(components, checked_at=NOW + timedelta(minutes=1))

    assert failed.overall_status is HealthStatus.CRITICAL
    assert failed.severity is HealthSeverity.CRITICAL
    assert failed.failed_check_names == ("backup", "sentry_delivery")


def test_component_failure_severity_and_time_are_fail_closed() -> None:
    components = _healthy_components()
    components["database"] = CloudHealthComponent(
        passed=False,
        failure_severity=HealthSeverity.WARNING,
        reason_code="database_failed",
        evidence={"observed": False},
    )
    with pytest.raises(ValueError, match="severity"):
        evaluate_cloud_components(components, checked_at=NOW)

    with pytest.raises(ValueError, match="UTC"):
        evaluate_cloud_components(
            _healthy_components(),
            checked_at=NOW.replace(tzinfo=None),
        )
