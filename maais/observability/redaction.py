"""Recursive redaction applied before logs or telemetry leave a process."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from structlog.typing import EventDict

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
UNSERIALIZABLE = "[UNSERIALIZABLE]"
MAX_REDACTION_DEPTH = 12
MAX_COLLECTION_ITEMS = 100
MAX_REDACTED_STRING_LENGTH = 16_384

_SENSITIVE_KEY_MARKERS = frozenset(
    {
        "account_equity",
        "access_key",
        "api_key",
        "authorization",
        "balance",
        "chat_id",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "csrf",
        "database_url",
        "dsn",
        "exchange_credential",
        "headers",
        "ip_address",
        "object_store_credential",
        "order_quantity",
        "passphrase",
        "password",
        "pepper",
        "position",
        "positions",
        "private_key",
        "quantity",
        "raw_operator_input",
        "request_body",
        "secret",
        "session_token",
        "telegram_credential",
        "token",
        "user_agent",
    }
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")
_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:access_token|api[_-]?key|authorization|csrf|dsn|password|secret|"
    r"session|token)=)[^&#\s]+"
)
_SENSITIVE_COOKIE_VALUE = re.compile(r"(?i)\b((?:auth|cookie|csrf|session|token)=)[^;\s]+")
_IPV4_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SECRET_TOKEN = re.compile(
    r"(?i)\b[a-z0-9_-]*(?:csrf|sentry|telegram|secret|token|password|api[_-]?key)"
    r"[a-z0-9_-]*\b"
)


def redact_event(
    _logger: Any,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    redacted = redact_value(event_dict)
    return redacted if isinstance(redacted, dict) else {"event": REDACTED}


def redact_value(value: object, *, key: str | None = None) -> object:
    return _redact_value(value, key=key, depth=0, seen=set())


def _redact_value(
    value: object,
    *,
    key: str | None,
    depth: int,
    seen: set[int],
) -> object:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if depth >= MAX_REDACTION_DEPTH:
        return TRUNCATED
    if isinstance(value, Mapping):
        return _redact_mapping(value, depth=depth, seen=seen)
    if isinstance(value, (list, tuple)):
        return _redact_collection(value, depth=depth, seen=seen)
    if isinstance(value, (set, frozenset)):
        ordered = sorted(value, key=_safe_text)
        return _redact_collection(ordered, depth=depth, seen=seen)
    if isinstance(value, bytes):
        return _redact_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(_safe_text(value))


def _redact_mapping(
    value: Mapping[object, object],
    *,
    depth: int,
    seen: set[int],
) -> dict[str, object]:
    identity = id(value)
    if identity in seen:
        return {"cycle": TRUNCATED}
    seen.add(identity)
    try:
        redacted: dict[str, object] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                redacted[TRUNCATED] = TRUNCATED
                break
            original_key = _safe_text(child_key)
            redacted[original_key] = _redact_value(
                child_value,
                key=original_key,
                depth=depth + 1,
                seen=seen,
            )
        return redacted
    except Exception:
        return {"value": UNSERIALIZABLE}
    finally:
        seen.remove(identity)


def _redact_collection(
    value: list[object] | tuple[object, ...] | set[object] | frozenset[object],
    *,
    depth: int,
    seen: set[int],
) -> list[object]:
    identity = id(value)
    if identity in seen:
        return [TRUNCATED]
    seen.add(identity)
    try:
        redacted = [
            _redact_value(item, key=None, depth=depth + 1, seen=seen)
            for item in list(value)[:MAX_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_COLLECTION_ITEMS:
            redacted.append(TRUNCATED)
        return redacted
    except Exception:
        return [UNSERIALIZABLE]
    finally:
        seen.remove(identity)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    padded = f"_{normalized}_"
    return any(f"_{marker}_" in padded for marker in _SENSITIVE_KEY_MARKERS)


def _redact_text(value: str) -> str:
    value = _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", value)
    value = _SENSITIVE_COOKIE_VALUE.sub(r"\1[REDACTED]", value)
    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    value = _BEARER.sub(REDACTED, value)
    value = _AWS_ACCESS_KEY.sub(REDACTED, value)
    value = _IPV4_ADDRESS.sub(REDACTED, value)
    value = _SECRET_TOKEN.sub(REDACTED, value)
    if len(value) <= MAX_REDACTED_STRING_LENGTH:
        return value
    return value[: MAX_REDACTED_STRING_LENGTH - 3] + "..."


def _safe_text(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return UNSERIALIZABLE
        ("private_key",)
