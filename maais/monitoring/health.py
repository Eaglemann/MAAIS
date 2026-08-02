"""System health tracker — heartbeat monitoring for all components."""

from __future__ import annotations

from datetime import datetime, timezone

from maais.core.logging import get_logger
from maais.monitoring.schemas import HealthStatus

logger = get_logger(__name__)

# Component considered stale after this many seconds without a heartbeat
_STALE_THRESHOLD_SECONDS = 60


class SystemHealthTracker:
    """Tracks heartbeats and error counts for each system component.

    Each component calls ping() on each processing cycle.
    is_healthy() returns False if no heartbeat received within the stale threshold.
    """

    def __init__(self, stale_seconds: int = _STALE_THRESHOLD_SECONDS) -> None:
        self._stale_seconds = stale_seconds
        self._statuses: dict[str, HealthStatus] = {}

    def ping(self, component: str) -> None:
        """Record a healthy heartbeat for a component."""
        if component not in self._statuses:
            self._statuses[component] = HealthStatus(
                component=component,
                is_healthy=True,
                last_heartbeat=None,
            )
        self._statuses[component].last_heartbeat = datetime.now(timezone.utc)
        self._statuses[component].is_healthy = True

    def record_error(self, component: str, error: str) -> None:
        """Record an error for a component."""
        if component not in self._statuses:
            self._statuses[component] = HealthStatus(
                component=component, is_healthy=False, last_heartbeat=None
            )
        status = self._statuses[component]
        status.error_count += 1
        status.last_error = error
        status.is_healthy = False
        logger.warning(
            "component_error", component=component, error=error, error_count=status.error_count
        )

    def get_status(self, component: str) -> HealthStatus:
        """Return current health status. Components never seen are unhealthy."""
        if component not in self._statuses:
            return HealthStatus(component=component, is_healthy=False, last_heartbeat=None)
        status = self._statuses[component]
        # Check for stale heartbeat
        if status.last_heartbeat is not None:
            age = (datetime.now(timezone.utc) - status.last_heartbeat).total_seconds()
            if age > self._stale_seconds:
                status.is_healthy = False
        return status

    def all_healthy(self) -> bool:
        """Return True only if all tracked components are healthy."""
        return all(self.get_status(c).is_healthy for c in self._statuses)

    def unhealthy_components(self) -> list[str]:
        return [c for c in self._statuses if not self.get_status(c).is_healthy]
