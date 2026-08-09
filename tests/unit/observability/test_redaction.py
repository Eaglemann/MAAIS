from __future__ import annotations

import json

from maais.observability.events import (
    MAX_EVENT_VALUE_LENGTH,
    MAX_EXCEPTION_CAUSES,
    MAX_EXCEPTION_MESSAGE_LENGTH,
    MAX_EXCEPTION_STACK_LENGTH,
    enforce_event_contract,
)
from maais.observability.redaction import (
    MAX_COLLECTION_ITEMS,
    MAX_REDACTED_STRING_LENGTH,
    REDACTED,
    TRUNCATED,
    UNSERIALIZABLE,
    redact_value,
)

CANARIES = (
    "postgresql://operator:db-secret@example.invalid/maais",  # pragma: allowlist secret
    "Bearer auth-secret",  # pragma: allowlist secret
    "csrf-secret",  # pragma: allowlist secret
    "sentry-auth-secret",  # pragma: allowlist secret
    "AKIAEXAMPLESECRET",  # pragma: allowlist secret
    "telegram-secret",  # pragma: allowlist secret
)


def assert_canaries_absent(value: str) -> None:
    for canary in CANARIES:
        assert canary not in value


def test_recursive_redaction_removes_canaries_from_arbitrary_nested_values() -> None:
    payload = {
        "plain": f"transport failed: {CANARIES[0]}",
        "nested": [
            {"message": CANARIES[1]},
            (CANARIES[2], CANARIES[3]),
            {CANARIES[4], CANARIES[5]},
        ],
    }

    redacted = redact_value(payload)
    serialized = json.dumps(redacted, sort_keys=True)

    assert_canaries_absent(serialized)
    assert REDACTED in serialized


def test_recursive_and_unrenderable_payloads_cannot_break_redaction() -> None:
    class Unrenderable:
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    cyclic: dict[str, object] = {"safe": "visible"}
    cyclic["self"] = cyclic

    redacted_cycle = redact_value(cyclic)
    redacted_collection = redact_value(list(range(MAX_COLLECTION_ITEMS + 10)))

    assert redacted_cycle["self"] == {"cycle": TRUNCATED}
    assert redact_value(Unrenderable()) == UNSERIALIZABLE
    assert len(redacted_collection) == MAX_COLLECTION_ITEMS + 1
    assert redacted_collection[-1] == TRUNCATED


def test_sensitive_keys_are_masked_before_any_off_platform_serialization() -> None:
    payload = {
        "database_url": "database-value",
        "authorization": "header-value",
        "cookie": "cookie-value",
        "csrf_token": "csrf-value",
        "sentry_dsn": "dsn-value",
        "object_store_secret": "storage-value",  # pragma: allowlist secret
        "telegram_token": "telegram-value",
        "exchange_api_key": "exchange-value",  # pragma: allowlist secret
        "account_equity": "10000",
        "positions": [{"symbol": "BTCUSDT"}],
        "order_quantity": "1.5",
        "raw_operator_input": "operator-value",
        "ip_address": "203.0.113.7",
        "user_agent": "browser-value",
        "request_body": {"arbitrary": "value"},
        "headers": {"x-forwarded-for": "203.0.113.7"},
        "artifact_replica_secret_key": "replica-value",  # pragma: allowlist secret
        "artifact_canonical_access_key": "canonical-value",
        "signing_private_key": "private-value",  # pragma: allowlist secret
        "telegram_chat_id": "chat-value",
        "session_pepper": "pepper-value",
        "safe": "visible",
    }

    redacted = redact_value(payload)

    assert redacted["safe"] == "visible"
    assert all(redacted[key] == REDACTED for key in payload if key != "safe")


def test_every_redacted_string_is_globally_bounded_for_future_telemetry() -> None:
    redacted = str(redact_value("x" * (MAX_REDACTED_STRING_LENGTH + 100)))

    assert len(redacted) == MAX_REDACTED_STRING_LENGTH
    assert redacted.endswith("...")


def test_urls_cookies_and_plain_ip_addresses_are_redacted_inside_messages() -> None:
    query_canary = "opaque-query-value"  # pragma: allowlist secret
    cookie_canary = "opaque-cookie-value"  # pragma: allowlist secret
    ip_canary = "203.0.113.7"
    message = (
        "GET https://example.invalid/path?access_token="
        f"{query_canary}&safe=visible session={cookie_canary} from {ip_canary}"
    )

    redacted = str(redact_value(message))

    assert query_canary not in redacted
    assert cookie_canary not in redacted
    assert ip_canary not in redacted
    assert "safe=visible" in redacted
    assert redacted.count(REDACTED) >= 3


def test_event_contract_drops_unknown_fields_and_bounds_allowed_references() -> None:
    long_value = "x" * (MAX_EVENT_VALUE_LENGTH + 100)
    event = enforce_event_contract(
        None,
        "info",
        {
            "event": long_value,
            "logger": long_value,
            "level": "info",
            "symbol": long_value,
            "reason_code": long_value,
            "unknown": "must-not-survive",
        },
    )

    assert "unknown" not in event
    assert event["event"].endswith("...")
    assert event["logger"].endswith("...")
    assert event["symbol"].endswith("...")
    assert event["reason_code"].endswith("...")
    assert all(
        len(str(event[field])) <= MAX_EVENT_VALUE_LENGTH
        for field in ("event", "logger", "symbol", "reason_code")
    )


def test_exception_contract_bounds_messages_stacks_and_cause_depth() -> None:
    event = enforce_event_contract(
        None,
        "error",
        {
            "event": "failure",
            "exception": {
                "type": "RuntimeError",
                "message": "m" * (MAX_EXCEPTION_MESSAGE_LENGTH + 100),
                "stack": "s" * (MAX_EXCEPTION_STACK_LENGTH + 100),
                "causes": [
                    {
                        "type": f"Cause{index}",
                        "message": "cause",
                        "stack": "stack",
                        "causes": [],
                    }
                    for index in range(MAX_EXCEPTION_CAUSES + 4)
                ],
            },
        },
    )

    exception = event["exception"]
    assert len(exception["message"]) == MAX_EXCEPTION_MESSAGE_LENGTH
    assert len(exception["stack"]) == MAX_EXCEPTION_STACK_LENGTH
    assert len(exception["causes"]) == MAX_EXCEPTION_CAUSES
