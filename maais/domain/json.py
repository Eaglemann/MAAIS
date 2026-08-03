from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias
from uuid import UUID

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
MutableJsonValue: TypeAlias = JsonScalar | list["MutableJsonValue"] | dict[str, "MutableJsonValue"]


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime values must be UTC-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def freeze_json(value: object) -> JsonValue:
    """Normalize supported values into an immutable, JSON-compatible tree."""

    if isinstance(value, Enum):
        return freeze_json(value.value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON decimals must be finite")
        if value.is_zero():
            return "0"
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON floats must be finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized[key] = freeze_json(item)
        return MappingProxyType(normalized)
    if isinstance(value, set | frozenset):
        frozen_items = [freeze_json(item) for item in value]
        return tuple(sorted(frozen_items, key=canonical_json_bytes))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def to_json_data(value: object) -> MutableJsonValue:
    """Convert supported values into mutable stdlib JSON containers."""

    frozen = freeze_json(value)

    def thaw(item: JsonValue) -> MutableJsonValue:
        if isinstance(item, Mapping):
            return {key: thaw(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [thaw(child) for child in item]
        return item

    return thaw(frozen)


def canonical_json_bytes(value: object) -> bytes:
    normalized = to_json_data(value)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
