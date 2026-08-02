from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maais.db.models.decisions import MarketFrameModel
from maais.db.models.operations import (
    DataQualityEvaluationModel,
    MarketCursorModel,
    MarketRecoveryRunModel,
)
from maais.db.repositories.events import EventRepository
from maais.domain.events import NewDomainEvent
from maais.domain.json import (
    JsonValue,
    MutableJsonValue,
    content_hash,
    freeze_json,
    to_json_data,
)
from maais.market_data.events import ClosedBarPayload
from maais.market_data.history import CommittedFrameSnapshot
from maais.market_data.integrity.state_machine import IntegrityAssessment, IntegrityCheck
from maais.market_data.recovery import (
    CursorStatus,
    GapRange,
    MarketCursor,
    RecoveryState,
    RecoveryStatus,
    RecoveryTransition,
)


class OperationalStateConflict(RuntimeError):
    pass


class StaleOperationalState(RuntimeError):
    pass


class ActiveRecoveryConflict(OperationalStateConflict):
    pass


@dataclass(frozen=True, slots=True)
class OperationalPersistResult:
    created: bool
    aggregate_id: UUID
    version: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class QualityPersistResult:
    created: bool
    market_frame_id: UUID
    row_count: int
    content_hash: str


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):
        raise TypeError("expected a JSON object")
    return normalized


def _event_object(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("expected an immutable JSON object")
    return normalized


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted datetime must be an ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("persisted datetime must be UTC-aware")
    return parsed


def _cursor_id(cursor: MarketCursor) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "maais://market-cursor/"
        f"{cursor.experiment_id}/{cursor.venue}/{cursor.stream}/{cursor.symbol}/{cursor.timeframe}",
    )


def _cursor_state(cursor: MarketCursor) -> dict[str, MutableJsonValue]:
    return _json_object(
        {
            "experiment_id": cursor.experiment_id,
            "venue": cursor.venue,
            "stream": cursor.stream,
            "symbol": cursor.symbol,
            "timeframe": cursor.timeframe,
            "event_id": cursor.event_id,
            "sequence": cursor.sequence,
            "venue_event_at": cursor.venue_event_at,
            "observed_at": cursor.observed_at,
            "bar_close_at": cursor.bar_close_at,
            "status": cursor.status,
            "version": cursor.version,
            "updated_at": cursor.updated_at,
        }
    )


def _cursor_from_state(state: Mapping[str, object]) -> MarketCursor:
    return MarketCursor(
        experiment_id=UUID(str(state["experiment_id"])),
        venue=str(state["venue"]),
        stream=str(state["stream"]),
        symbol=str(state["symbol"]),
        timeframe=str(state["timeframe"]),
        event_id=str(state["event_id"]),
        sequence=int(cast(str | int, state["sequence"])),
        venue_event_at=_parse_datetime(state["venue_event_at"]),
        observed_at=_parse_datetime(state["observed_at"]),
        bar_close_at=_parse_datetime(state["bar_close_at"]),
        status=CursorStatus(str(state["status"])),
        version=int(cast(str | int, state["version"])),
        updated_at=_parse_datetime(state["updated_at"]),
    )


def _recovery_state(recovery: RecoveryState) -> dict[str, MutableJsonValue]:
    return _json_object(
        {
            "recovery_id": recovery.recovery_id,
            "experiment_id": recovery.experiment_id,
            "gap": recovery.gap.to_dict(),
            "status": recovery.status,
            "attempt": recovery.attempt,
            "source_hash": recovery.source_hash,
            "failure_reason": recovery.failure_reason,
            "started_at": recovery.started_at,
            "changed_at": recovery.changed_at,
            "completed_at": recovery.completed_at,
            "version": recovery.version,
            "dispatched_through_sequence": recovery.dispatched_through_sequence,
            "dispatched_through_event_id": recovery.dispatched_through_event_id,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "event_at": event.event_at,
                    "payload": event.payload,
                }
                for event in recovery.events
            ],
        }
    )


def _recovery_from_state(state: Mapping[str, object]) -> RecoveryState:
    raw_gap = cast(Mapping[str, object], state["gap"])
    gap = GapRange(
        experiment_id=UUID(str(raw_gap["experiment_id"])),
        venue=str(raw_gap["venue"]),
        stream=str(raw_gap["stream"]),
        symbol=str(raw_gap["symbol"]),
        timeframe=str(raw_gap["timeframe"]),
        start_sequence=int(cast(str | int, raw_gap["start_sequence"])),
        end_sequence_exclusive=int(cast(str | int, raw_gap["end_sequence_exclusive"])),
        start_open_at=_parse_datetime(raw_gap["start_open_at"]),
        end_open_at_exclusive=_parse_datetime(raw_gap["end_open_at_exclusive"]),
        interval=timedelta(seconds=int(cast(str | int, raw_gap["interval_seconds"]))),
    )
    raw_events = cast(list[Mapping[str, object]], state["events"])
    events = tuple(
        RecoveryTransition(
            sequence=int(cast(str | int, event["sequence"])),
            event_type=str(event["event_type"]),
            event_at=_parse_datetime(event["event_at"]),
            payload=_event_object(event["payload"]),
        )
        for event in raw_events
    )
    return RecoveryState(
        recovery_id=UUID(str(state["recovery_id"])),
        experiment_id=UUID(str(state["experiment_id"])),
        gap=gap,
        status=RecoveryStatus(str(state["status"])),
        attempt=int(cast(str | int, state["attempt"])),
        source_hash=cast(str | None, state["source_hash"]),
        failure_reason=cast(str | None, state["failure_reason"]),
        started_at=_parse_datetime(state["started_at"]),
        changed_at=_parse_datetime(state["changed_at"]),
        completed_at=(
            _parse_datetime(state["completed_at"]) if state["completed_at"] is not None else None
        ),
        version=int(cast(str | int, state["version"])),
        events=events,
        dispatched_through_sequence=(
            int(cast(str | int, state["dispatched_through_sequence"]))
            if state.get("dispatched_through_sequence") is not None
            else None
        ),
        dispatched_through_event_id=(
            str(state["dispatched_through_event_id"])
            if state.get("dispatched_through_event_id") is not None
            else None
        ),
    )


def _new_event(
    *,
    aggregate_id: UUID,
    aggregate_type: str,
    event_type: str,
    payload: object,
    occurred_at: datetime,
) -> NewDomainEvent:
    return NewDomainEvent(
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        event_type=event_type,
        payload=_event_object(payload),
        metadata={"schema_revision": "0009"},
        occurred_at=occurred_at,
    )


class MarketDataRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def load_frame_history(
        self,
        experiment_id: UUID,
        symbol: str,
        timeframe: str,
        *,
        limit: int = 240,
    ) -> tuple[CommittedFrameSnapshot, ...]:
        if experiment_id.int == 0:
            raise ValueError("history experiment_id cannot be nil")
        if not symbol or symbol != symbol.upper() or not timeframe:
            raise ValueError("history query identity is invalid")
        if limit < 60:
            raise ValueError("history query must retain at least 60 bars")
        rows = (
            await self._session.scalars(
                select(MarketFrameModel)
                .where(
                    MarketFrameModel.experiment_id == experiment_id,
                    MarketFrameModel.symbol == symbol,
                    MarketFrameModel.timeframe == timeframe,
                )
                .order_by(MarketFrameModel.bar_close_at.desc())
                .limit(limit)
            )
        ).all()
        snapshots: list[CommittedFrameSnapshot] = []
        for row in reversed(rows):
            sequences: dict[str, int] = {}
            for name, raw in row.source_sequence_json.items():
                if not isinstance(raw, Mapping):
                    continue
                sequence = raw.get("sequence")
                if isinstance(sequence, int) and not isinstance(sequence, bool):
                    sequences[name] = sequence
            bar_snapshot = row.bar_snapshot_json
            has_complete_bar = all(
                name in bar_snapshot
                for name in (
                    "timeframe",
                    "bar_open_at",
                    "bar_close_at",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "quote_volume",
                    "trade_count",
                    "taker_buy_volume",
                    "taker_buy_quote_volume",
                    "closed",
                )
            )
            if has_complete_bar:
                raw_trade_count = bar_snapshot["trade_count"]
                if isinstance(raw_trade_count, bool) or not isinstance(raw_trade_count, (str, int)):
                    raise OperationalStateConflict("persisted bar trade_count is invalid")
                if bar_snapshot["closed"] is not True:
                    raise OperationalStateConflict("persisted feature history bar is not closed")
                bar = ClosedBarPayload(
                    timeframe=str(bar_snapshot["timeframe"]),
                    bar_open_at=_parse_datetime(bar_snapshot["bar_open_at"]),
                    bar_close_at=_parse_datetime(bar_snapshot["bar_close_at"]),
                    open=Decimal(str(bar_snapshot["open"])),
                    high=Decimal(str(bar_snapshot["high"])),
                    low=Decimal(str(bar_snapshot["low"])),
                    close=Decimal(str(bar_snapshot["close"])),
                    volume=Decimal(str(bar_snapshot["volume"])),
                    quote_volume=Decimal(str(bar_snapshot["quote_volume"])),
                    trade_count=int(raw_trade_count),
                    taker_buy_volume=Decimal(str(bar_snapshot["taker_buy_volume"])),
                    taker_buy_quote_volume=Decimal(str(bar_snapshot["taker_buy_quote_volume"])),
                    closed=True,
                )
            else:
                bar = ClosedBarPayload(
                    timeframe=row.timeframe,
                    bar_open_at=row.bar_open_at,
                    bar_close_at=row.bar_close_at,
                    open=Decimal(row.open),
                    high=Decimal(row.high),
                    low=Decimal(row.low),
                    close=Decimal(row.close),
                    volume=Decimal(row.volume),
                    quote_volume=Decimal("0"),
                    trade_count=0,
                    taker_buy_volume=Decimal("0"),
                    taker_buy_quote_volume=Decimal("0"),
                    closed=True,
                )
            snapshots.append(
                CommittedFrameSnapshot(
                    experiment_id=row.experiment_id,
                    frame_id=row.id,
                    symbol=row.symbol,
                    timeframe=row.timeframe,
                    bar=bar,
                    source_sequences=sequences,
                    content_hash=row.content_hash,
                )
            )
        return tuple(snapshots)

    async def record_cursor(self, cursor: MarketCursor) -> OperationalPersistResult:
        aggregate_id = _cursor_id(cursor)
        state = _cursor_state(cursor)
        state_hash = content_hash(state)
        inserted_id = await self._session.scalar(
            insert(MarketCursorModel)
            .values(**self._cursor_values(aggregate_id, cursor, state, state_hash))
            .on_conflict_do_nothing(index_elements=[MarketCursorModel.id])
            .returning(MarketCursorModel.id)
        )
        created = inserted_id is not None
        previous_version = 0
        if not created:
            existing = await self._session.scalar(
                select(MarketCursorModel)
                .where(MarketCursorModel.id == aggregate_id)
                .with_for_update()
            )
            if existing is None:
                raise RuntimeError("cursor identity disappeared after conflict")
            previous_version = existing.version
            if cursor.version < previous_version:
                raise StaleOperationalState("cursor state is older than persisted state")
            if cursor.version == previous_version:
                if existing.content_hash != state_hash:
                    raise OperationalStateConflict("cursor version has different content")
                return OperationalPersistResult(False, aggregate_id, cursor.version, state_hash)
            if cursor.version != previous_version + 1:
                raise StaleOperationalState("cursor versions must be contiguous")
            if cursor.sequence != existing.source_sequence + 1:
                raise StaleOperationalState("cursor source sequences must be contiguous")
            if cursor.bar_close_at <= existing.bar_close_at:
                raise StaleOperationalState("cursor bar close time must advance")
            for key, value in self._cursor_values(aggregate_id, cursor, state, state_hash).items():
                if key != "id":
                    setattr(existing, key, value)

        event_type = "market_cursor.created" if created else "market_cursor.advanced"
        await self._events.append(
            aggregate_id,
            "market_cursor",
            previous_version,
            (
                _new_event(
                    aggregate_id=aggregate_id,
                    aggregate_type="market_cursor",
                    event_type=event_type,
                    payload=state,
                    occurred_at=cursor.updated_at,
                ),
            ),
        )
        return OperationalPersistResult(created, aggregate_id, cursor.version, state_hash)

    async def record_quality(
        self,
        assessment: IntegrityAssessment,
        *,
        evaluated_at: datetime,
        required_checks: frozenset[IntegrityCheck],
    ) -> QualityPersistResult:
        if not isinstance(assessment.frame_id, UUID) or assessment.frame_id.int == 0:
            raise ValueError("quality assessment requires a non-nil UUID frame_id")
        if {result.check for result in assessment.results} != set(IntegrityCheck) or len(
            assessment.results
        ) != len(IntegrityCheck):
            raise ValueError("quality assessment requires exactly one result per integrity check")
        if not required_checks <= frozenset(IntegrityCheck):
            raise ValueError("required quality check set contains an unknown check")
        payload = {
            "frame_id": assessment.frame_id,
            "admission": assessment.admission,
            "quality_status": assessment.quality_status,
            "results": [result.to_dict() for result in assessment.results],
            "blocking_checks": assessment.blocking_checks,
        }
        expected_hash = content_hash(payload)
        if assessment.content_hash != expected_hash:
            raise OperationalStateConflict("quality assessment content hash is invalid")
        if await self._session.get(MarketFrameModel, assessment.frame_id) is None:
            raise LookupError("quality assessment market frame does not exist")

        created = 0
        for result in assessment.results:
            row_id = uuid5(
                NAMESPACE_URL,
                f"maais://market-quality/{assessment.frame_id}/{result.check.value}",
            )
            details = _json_object(result.details)
            row_payload = {
                "market_frame_id": assessment.frame_id,
                "check_name": result.check,
                "required": result.check in required_checks,
                "status": result.status,
                "reason_code": result.reason_code,
                "details": details,
                "evaluated_at": evaluated_at,
            }
            row_hash = content_hash(row_payload)
            inserted_id = await self._session.scalar(
                insert(DataQualityEvaluationModel)
                .values(
                    id=row_id,
                    market_frame_id=assessment.frame_id,
                    check_name=result.check.value,
                    required=result.check in required_checks,
                    status=result.status.value,
                    reason_code=result.reason_code,
                    details_json=details,
                    evaluated_at=evaluated_at,
                    content_hash=row_hash,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        DataQualityEvaluationModel.market_frame_id,
                        DataQualityEvaluationModel.check_name,
                    ]
                )
                .returning(DataQualityEvaluationModel.id)
            )
            if inserted_id is not None:
                created += 1
                continue
            existing = await self._session.scalar(
                select(DataQualityEvaluationModel).where(
                    DataQualityEvaluationModel.market_frame_id == assessment.frame_id,
                    DataQualityEvaluationModel.check_name == result.check.value,
                )
            )
            if existing is None or existing.content_hash != row_hash:
                raise OperationalStateConflict("quality check identity has different content")

        if created == 0:
            return QualityPersistResult(
                False,
                assessment.frame_id,
                len(assessment.results),
                assessment.content_hash,
            )
        if created != len(assessment.results):
            raise OperationalStateConflict("quality assessment is only partially persisted")
        await self._events.append(
            assessment.frame_id,
            "market_quality",
            0,
            (
                _new_event(
                    aggregate_id=assessment.frame_id,
                    aggregate_type="market_quality",
                    event_type="market_quality.evaluated",
                    payload=payload,
                    occurred_at=evaluated_at,
                ),
            ),
        )
        return QualityPersistResult(
            True,
            assessment.frame_id,
            len(assessment.results),
            assessment.content_hash,
        )

    async def get_cursor(
        self,
        experiment_id: UUID,
        venue: str,
        stream: str,
        symbol: str,
        timeframe: str,
        *,
        for_update: bool = False,
    ) -> MarketCursor:
        statement = select(MarketCursorModel).where(
            MarketCursorModel.experiment_id == experiment_id,
            MarketCursorModel.venue == venue,
            MarketCursorModel.stream == stream,
            MarketCursorModel.symbol == symbol,
            MarketCursorModel.timeframe == timeframe,
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        if row is None:
            raise LookupError("market cursor does not exist")
        return _cursor_from_state(cast(Mapping[str, object], row.state_json))

    async def record_recovery(self, recovery: RecoveryState) -> OperationalPersistResult:
        state = _recovery_state(recovery)
        state_hash = content_hash(state)
        inserted_id = await self._session.scalar(
            insert(MarketRecoveryRunModel)
            .values(**self._recovery_values(recovery, state, state_hash))
            .on_conflict_do_nothing()
            .returning(MarketRecoveryRunModel.id)
        )
        created = inserted_id is not None
        previous_version = 0
        if created:
            new_transitions = recovery.events
        else:
            existing = await self._session.scalar(
                select(MarketRecoveryRunModel)
                .where(MarketRecoveryRunModel.id == recovery.recovery_id)
                .with_for_update()
            )
            if existing is None:
                active = await self._session.scalar(
                    select(MarketRecoveryRunModel).where(
                        MarketRecoveryRunModel.experiment_id == recovery.experiment_id,
                        MarketRecoveryRunModel.venue == recovery.gap.venue,
                        MarketRecoveryRunModel.stream == recovery.gap.stream,
                        MarketRecoveryRunModel.symbol == recovery.gap.symbol,
                        MarketRecoveryRunModel.timeframe == recovery.gap.timeframe,
                        MarketRecoveryRunModel.status.in_(("detected", "backfilling")),
                    )
                )
                if active is not None:
                    raise ActiveRecoveryConflict("another recovery is active for this cursor")
                raise OperationalStateConflict("recovery identity conflicts with stored state")
            if (
                existing.experiment_id != recovery.experiment_id
                or existing.venue != recovery.gap.venue
                or existing.stream != recovery.gap.stream
                or existing.symbol != recovery.gap.symbol
                or existing.timeframe != recovery.gap.timeframe
                or existing.gap_start_sequence != recovery.gap.start_sequence
                or existing.gap_end_sequence_exclusive != recovery.gap.end_sequence_exclusive
                or existing.gap_start_open_at != recovery.gap.start_open_at
                or existing.gap_end_open_at_exclusive != recovery.gap.end_open_at_exclusive
                or existing.interval_seconds != int(recovery.gap.interval.total_seconds())
                or existing.started_at != recovery.started_at
            ):
                raise OperationalStateConflict("recovery immutable identity has changed")
            previous_version = existing.version
            if recovery.version < previous_version:
                raise StaleOperationalState("recovery state is older than persisted state")
            if recovery.version == previous_version:
                if existing.content_hash != state_hash:
                    raise OperationalStateConflict("recovery version has different content")
                return OperationalPersistResult(
                    False, recovery.recovery_id, recovery.version, state_hash
                )
            new_transitions = tuple(
                event for event in recovery.events if event.sequence > previous_version
            )
            if (
                recovery.version != previous_version + len(new_transitions)
                or not new_transitions
                or new_transitions[0].sequence != previous_version + 1
            ):
                raise StaleOperationalState("recovery transitions are not contiguous")
            for key, value in self._recovery_values(recovery, state, state_hash).items():
                if key != "id":
                    setattr(existing, key, value)

        await self._events.append(
            recovery.recovery_id,
            "market_recovery",
            previous_version,
            tuple(
                _new_event(
                    aggregate_id=recovery.recovery_id,
                    aggregate_type="market_recovery",
                    event_type=event.event_type,
                    payload=event.payload,
                    occurred_at=event.event_at,
                )
                for event in new_transitions
            ),
        )
        return OperationalPersistResult(created, recovery.recovery_id, recovery.version, state_hash)

    async def get_recovery(self, recovery_id: UUID) -> RecoveryState:
        row = await self._session.get(MarketRecoveryRunModel, recovery_id)
        if row is None:
            raise LookupError("market recovery does not exist")
        return _recovery_from_state(cast(Mapping[str, object], row.state_json))

    async def get_active_recoveries(self, experiment_id: UUID) -> tuple[RecoveryState, ...]:
        rows = (
            await self._session.scalars(
                select(MarketRecoveryRunModel)
                .where(
                    MarketRecoveryRunModel.experiment_id == experiment_id,
                    MarketRecoveryRunModel.status.in_(("detected", "backfilling")),
                )
                .order_by(MarketRecoveryRunModel.started_at, MarketRecoveryRunModel.id)
            )
        ).all()
        return tuple(
            _recovery_from_state(cast(Mapping[str, object], row.state_json)) for row in rows
        )

    async def get_blocking_recoveries(self, experiment_id: UUID) -> tuple[RecoveryState, ...]:
        rows = (
            await self._session.scalars(
                select(MarketRecoveryRunModel)
                .where(
                    MarketRecoveryRunModel.experiment_id == experiment_id,
                    MarketRecoveryRunModel.status.in_(("detected", "backfilling", "failed")),
                )
                .order_by(MarketRecoveryRunModel.started_at, MarketRecoveryRunModel.id)
            )
        ).all()
        return tuple(
            _recovery_from_state(cast(Mapping[str, object], row.state_json)) for row in rows
        )

    @staticmethod
    def _cursor_values(
        aggregate_id: UUID,
        cursor: MarketCursor,
        state: dict[str, MutableJsonValue],
        state_hash: str,
    ) -> dict[str, object]:
        return {
            "id": aggregate_id,
            "experiment_id": cursor.experiment_id,
            "venue": cursor.venue,
            "stream": cursor.stream,
            "symbol": cursor.symbol,
            "timeframe": cursor.timeframe,
            "event_id": cursor.event_id,
            "source_sequence": cursor.sequence,
            "venue_event_at": cursor.venue_event_at,
            "observed_at": cursor.observed_at,
            "bar_close_at": cursor.bar_close_at,
            "status": cursor.status.value,
            "version": cursor.version,
            "content_hash": state_hash,
            "state_json": state,
            "updated_at": cursor.updated_at,
        }

    @staticmethod
    def _recovery_values(
        recovery: RecoveryState,
        state: dict[str, MutableJsonValue],
        state_hash: str,
    ) -> dict[str, object]:
        return {
            "id": recovery.recovery_id,
            "experiment_id": recovery.experiment_id,
            "venue": recovery.gap.venue,
            "stream": recovery.gap.stream,
            "symbol": recovery.gap.symbol,
            "timeframe": recovery.gap.timeframe,
            "gap_start_sequence": recovery.gap.start_sequence,
            "gap_end_sequence_exclusive": recovery.gap.end_sequence_exclusive,
            "gap_start_open_at": recovery.gap.start_open_at,
            "gap_end_open_at_exclusive": recovery.gap.end_open_at_exclusive,
            "interval_seconds": int(recovery.gap.interval.total_seconds()),
            "status": recovery.status.value,
            "attempt": recovery.attempt,
            "source_hash": recovery.source_hash,
            "failure_reason": recovery.failure_reason,
            "started_at": recovery.started_at,
            "changed_at": recovery.changed_at,
            "completed_at": recovery.completed_at,
            "version": recovery.version,
            "state_json": state,
            "content_hash": state_hash,
        }
