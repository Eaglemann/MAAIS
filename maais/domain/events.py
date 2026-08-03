from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping
from uuid import UUID

from maais.domain.json import JsonValue, freeze_json


def _validate_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("occurred_at must be UTC-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class NewDomainEvent:
    aggregate_id: UUID
    aggregate_type: str
    event_type: str
    payload: Mapping[str, JsonValue]
    metadata: Mapping[str, JsonValue]
    occurred_at: datetime
    event_version: int = 1

    def __post_init__(self) -> None:
        if self.aggregate_id.int == 0:
            raise ValueError("aggregate_id cannot be nil")
        if not self.aggregate_type.strip():
            raise ValueError("aggregate_type cannot be empty")
        if "." not in self.event_type or not all(self.event_type.split(".")):
            raise ValueError("event_type must be a dotted non-empty name")
        if self.event_version < 1:
            raise ValueError("event_version must be at least one")
        _validate_utc(self.occurred_at)
        frozen_payload = freeze_json(self.payload)
        frozen_metadata = freeze_json(self.metadata)
        if not isinstance(frozen_payload, Mapping) or not isinstance(frozen_metadata, Mapping):
            raise TypeError("event payload and metadata must be JSON objects")
        object.__setattr__(self, "payload", frozen_payload)
        object.__setattr__(self, "metadata", frozen_metadata)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredDomainEvent(NewDomainEvent):
    id: UUID
    global_position: int
    stream_version: int

    def __post_init__(self) -> None:
        super(StoredDomainEvent, self).__post_init__()
        if self.id.int == 0:
            raise ValueError("event id cannot be nil")
        if self.global_position < 1:
            raise ValueError("global_position must be at least one")
        if self.stream_version < 1:
            raise ValueError("stream_version must be at least one")
