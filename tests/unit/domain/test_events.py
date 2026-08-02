from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.events import NewDomainEvent
from maais.domain.json import canonical_json_bytes, content_hash


def test_canonical_json_is_order_independent_and_lossless_for_decimal() -> None:
    left = {"b": Decimal("1.2300"), "a": [2, 1]}
    right = {"a": [2, 1], "b": Decimal("1.2300")}

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert b'"1.23"' in canonical_json_bytes(left)
    assert content_hash(left) == content_hash(right)


def test_event_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        NewDomainEvent(
            aggregate_id=UUID(int=1),
            aggregate_type="experiment",
            event_type="experiment.created",
            payload={},
            metadata={},
            occurred_at=datetime(2026, 1, 1),
        )


def test_event_rejects_invalid_version() -> None:
    with pytest.raises(ValueError, match="event_version"):
        NewDomainEvent(
            aggregate_id=UUID(int=1),
            aggregate_type="experiment",
            event_type="experiment.created",
            payload={},
            metadata={},
            occurred_at=datetime.now(timezone.utc),
            event_version=0,
        )


def test_event_payload_is_detached_and_immutable() -> None:
    payload = {"nested": [1, 2]}
    event = NewDomainEvent(
        aggregate_id=UUID(int=1),
        aggregate_type="experiment",
        event_type="experiment.created",
        payload=payload,
        metadata={},
        occurred_at=datetime.now(timezone.utc),
    )

    payload["nested"].append(3)
    assert event.payload["nested"] == (1, 2)
    with pytest.raises(TypeError):
        event.payload["new"] = "value"  # type: ignore[index]


def test_decimal_hash_ignores_storage_scale_padding() -> None:
    assert content_hash({"value": Decimal("1.23")}) == content_hash(
        {"value": Decimal("1.230000000000000000")}
    )
