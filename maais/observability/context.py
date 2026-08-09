"""Scoped structlog context binding with guaranteed restoration."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from structlog.contextvars import bound_contextvars

from maais.observability.events import TelemetryContext

_OPERATION_CONTEXT_FIELDS = frozenset(
    {
        "correlation_id",
        "operation_id",
        "experiment_ref",
        "decision_cycle_id",
        "symbol",
    }
)


@contextmanager
def bind_telemetry_context(context: TelemetryContext) -> Iterator[None]:
    """Bind immutable service identity for the lifetime of one process boundary."""
    with bound_contextvars(**context.to_log_fields()):
        yield


@contextmanager
def bind_log_context(**values: object) -> Iterator[None]:
    """Bind bounded operation references and restore any prior values on exit."""
    unknown = set(values) - _OPERATION_CONTEXT_FIELDS
    if unknown:
        raise ValueError("unsupported log context fields: " + ", ".join(sorted(unknown)))
    normalized: dict[str, str] = {}
    for key, value in values.items():
        if value is None:
            continue
        normalized_value = _context_value(value)
        if normalized_value:
            normalized[key] = normalized_value
    with bound_contextvars(**normalized):
        yield


def _context_value(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    return str(value)
