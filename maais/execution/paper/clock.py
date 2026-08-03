from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


@dataclass(frozen=True, slots=True)
class OrderEligibility:
    decided_at: datetime
    latency: timedelta
    eligible_at: datetime


@dataclass(frozen=True, slots=True)
class ObservedEvent:
    event_id: str
    observed_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "observed_at")
        if not self.event_id:
            raise ValueError("event_id is required")
        if self.sequence < 0:
            raise ValueError("sequence must be nonnegative")


class DeterministicClock:
    """Clock boundary that makes event eligibility explicit and replayable."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now

    def now(self) -> datetime:
        value = self._now()
        require_utc(value, "now")
        return value.astimezone(timezone.utc)

    def eligibility(self, decided_at: datetime, latency: timedelta) -> OrderEligibility:
        require_utc(decided_at, "decided_at")
        if latency <= timedelta(0):
            raise ValueError("latency must be positive")
        return OrderEligibility(decided_at, latency, decided_at + latency)

    def first_eligible(
        self,
        eligibility: OrderEligibility,
        events: Iterable[ObservedEvent],
    ) -> ObservedEvent:
        candidates = (event for event in events if event.observed_at > eligibility.eligible_at)
        try:
            return min(candidates, key=lambda event: (event.observed_at, event.sequence))
        except ValueError as exc:
            raise LookupError("no eligible observed market event") from exc
