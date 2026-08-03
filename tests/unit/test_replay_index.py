from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from uuid import UUID

from maais.db.models.ledger import EventStreamModel
from maais.db.replay import _index_event_streams


class _OnePassStreams:
    def __init__(self, streams: tuple[EventStreamModel, ...]) -> None:
        self._streams = streams
        self._iterated = False

    def __iter__(self) -> Iterator[EventStreamModel]:
        if self._iterated:
            raise AssertionError("event streams must be indexed in one pass")
        self._iterated = True
        return iter(self._streams)


def test_event_stream_indexes_resolve_ids_and_aggregates_in_one_pass() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    decision_stream = EventStreamModel(
        id=UUID(int=1),
        aggregate_id=UUID(int=11),
        aggregate_type="decision_cycle",
        current_version=10,
        created_at=now,
        updated_at=now,
    )
    experiment_stream = EventStreamModel(
        id=UUID(int=2),
        aggregate_id=UUID(int=22),
        aggregate_type="experiment",
        current_version=2,
        created_at=now,
        updated_at=now,
    )

    by_id, by_aggregate = _index_event_streams(
        _OnePassStreams((decision_stream, experiment_stream))
    )

    assert by_id == {
        decision_stream.id: decision_stream,
        experiment_stream.id: experiment_stream,
    }
    assert by_aggregate == {
        ("decision_cycle", decision_stream.aggregate_id): decision_stream,
        ("experiment", experiment_stream.aggregate_id): experiment_stream,
    }
