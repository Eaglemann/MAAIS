"""Versioned MAAIS log event contract and exception normalization."""

from __future__ import annotations

import json
import math
import sys
import traceback
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from structlog.typing import EventDict

EVENT_SCHEMA_VERSION = 1
MAX_EVENT_VALUE_LENGTH = 512
MAX_EXCEPTION_MESSAGE_LENGTH = 2_048
MAX_EXCEPTION_STACK_LENGTH = 8_192
MAX_EXCEPTION_CAUSES = 8

ALLOWED_COMMON_FIELDS = frozenset(
    {
        "event_schema_version",
        "timestamp",
        "level",
        "logger",
        "event",
        "service_role",
        "environment",
        "release",
        "candidate_hash",
        "deployment_id",
        "replica_id",
        "region",
        "boot_id",
        "correlation_id",
        "operation_id",
        "experiment_ref",
        "decision_cycle_id",
        "symbol",
        "outcome",
        "duration_ms",
        "retry_count",
        "reason_code",
        "error_code",
        "exception",
    }
)


@dataclass(frozen=True, slots=True)
class TelemetryContext:
    service_role: str
    environment: str
    release: str
    candidate_hash: str
    deployment_id: str
    replica_id: str
    region: str
    boot_id: UUID

    def to_log_fields(self) -> dict[str, str]:
        return {
            "service_role": self.service_role,
            "environment": self.environment,
            "release": self.release,
            "candidate_hash": self.candidate_hash,
            "deployment_id": self.deployment_id,
            "replica_id": self.replica_id,
            "region": self.region,
            "boot_id": str(self.boot_id),
        }


def normalize_exception(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    exc_info = event_dict.pop("exc_info", None)
    exception = _exception_from_info(exc_info)
    if exception is not None:
        event_dict["exception"] = _exception_payload(exception)
    return event_dict


def enforce_event_contract(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    contracted: dict[str, Any] = {}
    for key in ALLOWED_COMMON_FIELDS:
        if key not in event_dict:
            continue
        value = event_dict[key]
        if key == "exception":
            normalized_exception = _bounded_exception(value)
            if normalized_exception:
                contracted[key] = normalized_exception
        elif key == "duration_ms":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric = float(value)
                if math.isfinite(numeric) and numeric >= 0:
                    contracted[key] = value
        elif key == "retry_count":
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                contracted[key] = value
        elif value is None or isinstance(value, bool):
            contracted[key] = value
        else:
            contracted[key] = _truncate(str(value), MAX_EVENT_VALUE_LENGTH)
    return contracted


def add_event_schema_version(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    event_dict["event_schema_version"] = EVENT_SCHEMA_VERSION
    return event_dict


def remove_processor_metadata(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    event_dict.pop("_record", None)
    event_dict.pop("_from_structlog", None)
    return event_dict


def render_console_exception(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    exception = event_dict.get("exception")
    if isinstance(exception, dict):
        event_dict["exception"] = json.dumps(
            exception,
            sort_keys=True,
            separators=(",", ":"),
        )
    return event_dict


def _exception_from_info(exc_info: object) -> BaseException | None:
    if exc_info is True:
        return sys.exc_info()[1]
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        value = exc_info[1]
        return value if isinstance(value, BaseException) else None
    return None


def _exception_payload(exception: BaseException) -> dict[str, object]:
    causes: list[dict[str, object]] = []
    seen = {id(exception)}
    cause = _next_cause(exception)
    while cause is not None and len(causes) < MAX_EXCEPTION_CAUSES and id(cause) not in seen:
        seen.add(id(cause))
        causes.append(_single_exception_payload(cause))
        cause = _next_cause(cause)
    return {**_single_exception_payload(exception), "causes": causes}


def _single_exception_payload(exception: BaseException) -> dict[str, object]:
    try:
        stack = "".join(
            traceback.TracebackException.from_exception(
                exception,
                capture_locals=False,
            ).format(chain=False)
        )
    except Exception:
        stack = "[UNSERIALIZABLE]"
    return {
        "type": type(exception).__name__,
        "message": _safe_exception_message(exception),
        "stack": stack,
    }


def _next_cause(exception: BaseException) -> BaseException | None:
    if exception.__cause__ is not None:
        return exception.__cause__
    if not exception.__suppress_context__:
        return exception.__context__
    return None


def _bounded_exception(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    exception_type = _truncate(str(value.get("type", "Exception")), MAX_EVENT_VALUE_LENGTH)
    message = _truncate(str(value.get("message", "")), MAX_EXCEPTION_MESSAGE_LENGTH)
    stack = _truncate(str(value.get("stack", "")), MAX_EXCEPTION_STACK_LENGTH)
    causes_value = value.get("causes", [])
    causes: list[dict[str, object]] = []
    if isinstance(causes_value, list):
        for cause in causes_value[:MAX_EXCEPTION_CAUSES]:
            bounded = _bounded_exception(cause)
            if bounded:
                bounded.pop("causes", None)
                causes.append(bounded)
    return {
        "type": exception_type,
        "message": message,
        "stack": stack,
        "causes": causes,
    }


def _truncate(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 3] + "..."


def _safe_exception_message(exception: BaseException) -> str:
    try:
        return str(exception)
    except Exception:
        return "[UNSERIALIZABLE]"
