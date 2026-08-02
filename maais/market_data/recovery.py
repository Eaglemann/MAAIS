from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from maais.domain.json import JsonValue, content_hash, freeze_json
from maais.market_data.events import ClosedBarPayload, MarketEventKind, ObservedMarketEvent


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


class BackfillValidationError(ValueError):
    pass


class CursorStatus(StrEnum):
    ACTIVE = "active"
    RECOVERING = "recovering"
    HALTED = "halted"


class RecoveryStatus(StrEnum):
    DETECTED = "detected"
    BACKFILLING = "backfilling"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MarketCursor:
    experiment_id: UUID
    venue: str
    stream: str
    symbol: str
    timeframe: str
    event_id: str
    sequence: int
    venue_event_at: datetime
    observed_at: datetime
    bar_close_at: datetime
    status: CursorStatus
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0:
            raise ValueError("cursor experiment_id cannot be nil")
        if not all((self.venue, self.stream, self.symbol, self.timeframe, self.event_id)):
            raise ValueError("cursor identity fields are required")
        if self.symbol != self.symbol.upper():
            raise ValueError("cursor symbol must be uppercase")
        if self.sequence < 0 or self.version <= 0:
            raise ValueError("cursor sequence and version must be positive state")
        for value, field in (
            (self.venue_event_at, "venue_event_at"),
            (self.observed_at, "observed_at"),
            (self.bar_close_at, "bar_close_at"),
            (self.updated_at, "updated_at"),
        ):
            _require_utc(value, field)
        if self.venue_event_at > self.observed_at or self.bar_close_at > self.observed_at:
            raise ValueError("cursor observation cannot precede venue event or bar close")
        if self.updated_at < self.observed_at:
            raise ValueError("cursor update cannot precede observation")

    @classmethod
    def create(
        cls,
        *,
        experiment_id: UUID,
        venue: str,
        stream: str,
        symbol: str,
        timeframe: str,
        event_id: str,
        sequence: int | None,
        venue_event_at: datetime,
        observed_at: datetime,
        bar_close_at: datetime,
        updated_at: datetime,
    ) -> MarketCursor:
        if sequence is None:
            raise ValueError("closed-bar cursor requires a sequence")
        return cls(
            experiment_id=experiment_id,
            venue=venue,
            stream=stream,
            symbol=symbol,
            timeframe=timeframe,
            event_id=event_id,
            sequence=sequence,
            venue_event_at=venue_event_at,
            observed_at=observed_at,
            bar_close_at=bar_close_at,
            status=CursorStatus.ACTIVE,
            version=1,
            updated_at=updated_at,
        )

    def advance_closed_bar(self, event: ObservedMarketEvent) -> MarketCursor:
        if event.kind is not MarketEventKind.CLOSED_BAR or not isinstance(
            event.payload, ClosedBarPayload
        ):
            raise ValueError("cursor can advance only from a closed-bar event")
        if (
            event.venue != self.venue
            or event.stream != self.stream
            or event.symbol != self.symbol
            or event.payload.timeframe != self.timeframe
        ):
            raise ValueError("cursor and closed-bar identity differ")
        if not event.payload.closed:
            raise ValueError("cursor cannot advance from an open bar")
        if event.sequence is None:
            raise ValueError("closed-bar cursor requires a sequence")
        if event.sequence <= self.sequence or event.payload.bar_close_at <= self.bar_close_at:
            raise ValueError("cursor regression or duplicate")
        if event.sequence != self.sequence + 1:
            raise ValueError("cursor sequence gap requires recovery")
        if event.payload.bar_open_at != self.bar_close_at:
            raise ValueError("cursor bar gap requires recovery")
        return replace(
            self,
            event_id=event.event_id,
            sequence=event.sequence,
            venue_event_at=event.venue_event_at,
            observed_at=event.observed_at,
            bar_close_at=event.payload.bar_close_at,
            status=CursorStatus.ACTIVE,
            version=self.version + 1,
            updated_at=event.observed_at,
        )


@dataclass(frozen=True, slots=True)
class GapRange:
    experiment_id: UUID
    venue: str
    stream: str
    symbol: str
    timeframe: str
    start_sequence: int
    end_sequence_exclusive: int
    start_open_at: datetime
    end_open_at_exclusive: datetime
    interval: timedelta

    def __post_init__(self) -> None:
        _require_utc(self.start_open_at, "start_open_at")
        _require_utc(self.end_open_at_exclusive, "end_open_at_exclusive")
        if self.interval <= timedelta(0):
            raise ValueError("gap interval must be positive")
        if self.start_sequence < 0 or self.end_sequence_exclusive <= self.start_sequence:
            raise ValueError("gap sequence range must contain at least one event")
        if self.end_open_at_exclusive <= self.start_open_at:
            raise ValueError("gap range must contain at least one interval")
        if (self.end_open_at_exclusive - self.start_open_at) % self.interval:
            raise ValueError("gap range must align to its interval")
        if self.end_sequence_exclusive - self.start_sequence != self.missing_count:
            raise ValueError("gap time and sequence coverage differ")

    @property
    def missing_open_times(self) -> tuple[datetime, ...]:
        count = self.missing_count
        return tuple(self.start_open_at + self.interval * index for index in range(count))

    @property
    def missing_count(self) -> int:
        return (self.end_open_at_exclusive - self.start_open_at) // self.interval

    @property
    def missing_sequences(self) -> tuple[int, ...]:
        return tuple(range(self.start_sequence, self.end_sequence_exclusive))

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "venue": self.venue,
            "stream": self.stream,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_sequence": self.start_sequence,
            "end_sequence_exclusive": self.end_sequence_exclusive,
            "start_open_at": self.start_open_at,
            "end_open_at_exclusive": self.end_open_at_exclusive,
            "interval_seconds": int(self.interval.total_seconds()),
            "missing_count": self.missing_count,
        }


def detect_closed_bar_gap(
    cursor: MarketCursor,
    candidate: ObservedMarketEvent,
) -> GapRange | None:
    if candidate.kind is not MarketEventKind.CLOSED_BAR or not isinstance(
        candidate.payload, ClosedBarPayload
    ):
        raise ValueError("gap detection requires a closed-bar candidate")
    bar = candidate.payload
    if (
        candidate.venue != cursor.venue
        or candidate.stream != cursor.stream
        or candidate.symbol != cursor.symbol
        or bar.timeframe != cursor.timeframe
    ):
        raise ValueError("candidate and cursor identity differ")
    if not bar.closed:
        raise ValueError("gap detection requires a closed candidate bar")
    if bar.bar_open_at < cursor.bar_close_at:
        raise ValueError("candidate bar regresses behind cursor")
    if candidate.sequence is None or candidate.sequence <= cursor.sequence:
        raise ValueError("candidate sequence regresses behind cursor")
    if bar.bar_open_at == cursor.bar_close_at and candidate.sequence == cursor.sequence + 1:
        return None
    if bar.bar_open_at == cursor.bar_close_at:
        raise ValueError("sequence gap exists without a closed-bar time gap")
    interval = bar.bar_close_at - bar.bar_open_at
    return GapRange(
        experiment_id=cursor.experiment_id,
        venue=cursor.venue,
        stream=cursor.stream,
        symbol=cursor.symbol,
        timeframe=cursor.timeframe,
        start_sequence=cursor.sequence + 1,
        end_sequence_exclusive=candidate.sequence,
        start_open_at=cursor.bar_close_at,
        end_open_at_exclusive=bar.bar_open_at,
        interval=interval,
    )


@dataclass(frozen=True, slots=True)
class BackfillBatch:
    gap: GapRange
    events: tuple[ObservedMarketEvent, ...]
    content_hash: str


def validate_backfill(
    gap: GapRange,
    events: tuple[ObservedMarketEvent, ...],
) -> BackfillBatch:
    if len(events) != gap.missing_count:
        raise BackfillValidationError(
            f"backfill coverage differs: expected {gap.missing_count}, got {len(events)}"
        )
    actual_open_times: list[datetime] = []
    actual_sequences: list[int] = []
    for event in events:
        if event.kind is not MarketEventKind.CLOSED_BAR or not isinstance(
            event.payload, ClosedBarPayload
        ):
            raise BackfillValidationError("backfill contains a non-bar event")
        bar = event.payload
        if (
            event.venue != gap.venue
            or event.stream != gap.stream
            or event.symbol != gap.symbol
            or bar.timeframe != gap.timeframe
        ):
            raise BackfillValidationError("backfill identity differs from the gap")
        if not bar.closed:
            raise BackfillValidationError("backfill contains an open rather than closed bar")
        if event.sequence is None:
            raise BackfillValidationError("backfill contains an event without a sequence")
        actual_open_times.append(bar.bar_open_at)
        actual_sequences.append(event.sequence)
    if actual_open_times != sorted(actual_open_times):
        raise BackfillValidationError("backfill events are not ordered")
    if len(set(actual_open_times)) != len(actual_open_times):
        raise BackfillValidationError("backfill contains duplicate bar opens")
    if tuple(actual_open_times) != gap.missing_open_times:
        raise BackfillValidationError("backfill coverage does not exactly match the gap")
    if tuple(actual_sequences) != gap.missing_sequences:
        raise BackfillValidationError("backfill sequence coverage does not exactly match the gap")
    normalized = {"gap": gap.to_dict(), "events": [event.to_dict() for event in events]}
    return BackfillBatch(gap=gap, events=events, content_hash=content_hash(normalized))


@dataclass(frozen=True, slots=True)
class RecoveryTransition:
    sequence: int
    event_type: str
    event_at: datetime
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.sequence <= 0 or "." not in self.event_type:
            raise ValueError("recovery transition identity is invalid")
        _require_utc(self.event_at, "event_at")
        object.__setattr__(self, "payload", _payload(self.payload))


@dataclass(frozen=True, slots=True)
class RecoveryState:
    recovery_id: UUID
    experiment_id: UUID
    gap: GapRange
    status: RecoveryStatus
    attempt: int
    source_hash: str | None
    failure_reason: str | None
    started_at: datetime
    changed_at: datetime
    completed_at: datetime | None
    version: int
    events: tuple[RecoveryTransition, ...]

    def __post_init__(self) -> None:
        if self.recovery_id.int == 0 or self.experiment_id.int == 0:
            raise ValueError("recovery UUIDs cannot be nil")
        if self.gap.experiment_id != self.experiment_id:
            raise ValueError("gap and recovery experiment differ")
        if self.attempt < 0 or self.version <= 0:
            raise ValueError("recovery attempt and version are invalid")
        _require_utc(self.started_at, "started_at")
        _require_utc(self.changed_at, "changed_at")
        if self.changed_at < self.started_at:
            raise ValueError("recovery change time cannot precede start")
        if self.completed_at is not None:
            _require_utc(self.completed_at, "completed_at")
        if self.status is RecoveryStatus.COMPLETED:
            if self.completed_at != self.changed_at or not self.source_hash:
                raise ValueError("completed recovery requires completion time and source hash")
            if len(self.source_hash) != 64:
                raise ValueError("recovery source hash must be SHA-256")
        elif self.completed_at is not None:
            raise ValueError("only a completed recovery can have a completion time")
        if self.status is RecoveryStatus.FAILED and not self.failure_reason:
            raise ValueError("failed recovery requires a reason")
        if self.status is not RecoveryStatus.FAILED and self.failure_reason is not None:
            raise ValueError("only a failed recovery can have a failure reason")
        if len(self.events) != self.version or tuple(
            event.sequence for event in self.events
        ) != tuple(range(1, self.version + 1)):
            raise ValueError("recovery event history must be contiguous")
        if self.events[0].event_type != "market_recovery.detected":
            raise ValueError("recovery history must begin with detection")
        if self.events[-1].event_at != self.changed_at:
            raise ValueError("recovery change time must match the latest event")
        if any(
            current.event_at < previous.event_at
            for previous, current in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("recovery event time cannot regress")

    @property
    def entries_blocked(self) -> bool:
        return self.status is not RecoveryStatus.COMPLETED

    @classmethod
    def create(
        cls,
        *,
        recovery_id: UUID,
        experiment_id: UUID,
        gap: GapRange,
        started_at: datetime,
    ) -> RecoveryState:
        _require_utc(started_at, "started_at")
        if recovery_id.int == 0 or experiment_id.int == 0:
            raise ValueError("recovery UUIDs cannot be nil")
        if gap.experiment_id != experiment_id:
            raise ValueError("gap and recovery experiment differ")
        event = RecoveryTransition(
            sequence=1,
            event_type="market_recovery.detected",
            event_at=started_at,
            payload=_payload(gap.to_dict()),
        )
        return cls(
            recovery_id=recovery_id,
            experiment_id=experiment_id,
            gap=gap,
            status=RecoveryStatus.DETECTED,
            attempt=0,
            source_hash=None,
            failure_reason=None,
            started_at=started_at,
            changed_at=started_at,
            completed_at=None,
            version=1,
            events=(event,),
        )

    def begin(self, changed_at: datetime) -> RecoveryState:
        if self.status is not RecoveryStatus.DETECTED:
            raise RuntimeError("only detected recovery can begin backfilling")
        return self._advance(
            status=RecoveryStatus.BACKFILLING,
            event_type="market_recovery.backfill_started",
            event_at=changed_at,
            payload={"attempt": self.attempt + 1},
            attempt=self.attempt + 1,
        )

    def complete(self, batch: BackfillBatch, completed_at: datetime) -> RecoveryState:
        if self.status is not RecoveryStatus.BACKFILLING:
            raise RuntimeError("only backfilling recovery can complete")
        if batch.gap != self.gap:
            raise ValueError("backfill batch belongs to another gap")
        return self._advance(
            status=RecoveryStatus.COMPLETED,
            event_type="market_recovery.completed",
            event_at=completed_at,
            payload={"source_hash": batch.content_hash, "events": len(batch.events)},
            source_hash=batch.content_hash,
            completed_at=completed_at,
        )

    def fail(self, reason: str, failed_at: datetime) -> RecoveryState:
        if self.status not in {RecoveryStatus.DETECTED, RecoveryStatus.BACKFILLING}:
            raise RuntimeError("only an active recovery can fail")
        if not reason:
            raise ValueError("recovery failure reason is required")
        return self._advance(
            status=RecoveryStatus.FAILED,
            event_type="market_recovery.failed",
            event_at=failed_at,
            payload={"reason": reason},
            failure_reason=reason,
        )

    def _advance(
        self,
        *,
        status: RecoveryStatus,
        event_type: str,
        event_at: datetime,
        payload: object,
        **changes: object,
    ) -> RecoveryState:
        _require_utc(event_at, "event_at")
        if event_at < self.changed_at:
            raise ValueError("recovery event time cannot regress")
        event = RecoveryTransition(
            sequence=self.version + 1,
            event_type=event_type,
            event_at=event_at,
            payload=_payload(payload),
        )
        return replace(
            self,
            status=status,
            changed_at=event_at,
            version=self.version + 1,
            events=(*self.events, event),
            **changes,
        )


def _payload(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("recovery event payload must be an object")
    return normalized
