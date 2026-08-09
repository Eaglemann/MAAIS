from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
import sentry_sdk
from pydantic import SecretStr
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import HttpTransport, Transport

from maais.config.cloud import ServiceRole
from maais.config.observability import ObservabilitySettings
from maais.observability.sentry import (
    _ConfirmingHttpTransport,
    capture_terminal_exception,
    initialize_backend_sentry,
    shutdown_backend_sentry,
)
from tests.unit.observability.test_redaction import CANARIES, assert_canaries_absent


class _CaptureTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.envelopes: list[object] = []

    def capture_envelope(self, envelope: object) -> None:
        self.envelopes.append(envelope)


class _FailingTransport(Transport):
    def capture_envelope(self, envelope: object) -> None:
        del envelope
        raise RuntimeError("Sentry transport unavailable")


class _UnconfirmedTransport(_CaptureTransport):
    def delivery_confirmed(self, event_id: str) -> bool:
        assert event_id
        return False


@pytest.fixture(autouse=True)
def _isolated_sentry_runtime() -> Iterator[None]:
    shutdown_backend_sentry()
    yield
    shutdown_backend_sentry()


def _settings() -> ObservabilitySettings:
    return ObservabilitySettings(
        service_role=ServiceRole.WORKER,
        environment="qualification",
        release="a" * 40,
        backend_dsn=SecretStr("https://public@example.invalid/1"),
    )


def test_captured_sentry_envelope_removes_every_seeded_canary_and_pii_surface() -> None:
    transport = _CaptureTransport()
    runtime = initialize_backend_sentry(_settings(), transport=transport)

    sentry_sdk.set_user(
        {
            "id": CANARIES[2],
            "ip_address": "203.0.113.7",
            "user_agent": CANARIES[3],
        }
    )
    sentry_sdk.set_tag("authorization", CANARIES[1])
    sentry_sdk.set_context(
        "trading",
        {
            "account_equity": "10000",
            "positions": [CANARIES[4]],
            "safe": "visible",
        },
    )
    sentry_sdk.add_breadcrumb(
        category="request",
        message=CANARIES[5],
        data={"cookie": CANARIES[2], "safe": "visible"},
    )
    sentry_sdk.capture_event(
        {
            "level": "error",
            "message": f"request failed: {CANARIES[0]}",
            "request": {
                "headers": {"Authorization": CANARIES[1]},
                "cookies": {"session": CANARIES[2]},
                "data": {"request_body": CANARIES[3]},
                "env": {"REMOTE_ADDR": "203.0.113.7"},
            },
            "tags": {"sentry_token": CANARIES[3]},
            "contexts": {"unsafe": {"raw_operator_input": CANARIES[5]}},
            "user": {"email": CANARIES[4]},
        }
    )
    try:
        raise RuntimeError(f"terminal failure: {CANARIES[5]}")
    except RuntimeError as exc:
        assert capture_terminal_exception(
            exc,
            event="worker_terminal_failure",
            error_code="worker_unhandled_exception",
            outcome="halted",
            tags={"phase": CANARIES[2]},
            contexts={"secondary": {"database_url": CANARIES[0]}},
        )
    assert runtime.flush(timeout=1.0)

    serialized = b"\n".join(
        envelope.serialize()  # type: ignore[union-attr]
        for envelope in transport.envelopes
    ).decode("utf-8")
    assert_canaries_absent(serialized)
    assert "203.0.113.7" not in serialized
    assert '"user"' not in serialized
    for envelope in transport.envelopes:
        event = envelope.get_event()  # type: ignore[union-attr]
        if event is not None:
            assert "request" not in event
            assert "user" not in event
            assert event["tags"]["maais.service_role"] == "worker"
            assert event["tags"]["maais.deployment_target"] == "local"


def test_backend_sentry_uses_exact_release_and_privacy_safe_zero_sampling() -> None:
    transport = _CaptureTransport()

    first = initialize_backend_sentry(_settings(), transport=transport)
    second = initialize_backend_sentry(_settings(), transport=transport)

    assert first is second
    assert first.enabled
    options = sentry_sdk.get_client().options
    assert options["environment"] == "qualification"
    assert options["release"] == "a" * 40
    assert options["send_default_pii"] is False
    assert options["sample_rate"] == 1.0
    assert options["traces_sample_rate"] == 0.0
    assert options["profiles_sample_rate"] == 0.0
    assert options["max_request_body_size"] == "never"
    assert options["include_local_variables"] is False
    assert options["include_source_context"] is False


def test_conflicting_second_initialization_is_disabled_without_replacing_client() -> None:
    transport = _CaptureTransport()
    first = initialize_backend_sentry(_settings(), transport=transport)
    conflicting = _settings().model_copy(update={"release": "b" * 40})

    second = initialize_backend_sentry(conflicting, transport=transport)

    assert first.enabled
    assert second.redacted_summary() == {
        "enabled": False,
        "initialization_error": "configuration_conflict",
    }
    assert sentry_sdk.get_client().options["release"] == "a" * 40


def test_initialization_failure_is_redacted_and_does_not_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sentry_sdk,
        "init",
        lambda **_: (_ for _ in ()).throw(RuntimeError("dsn-secret")),
    )

    runtime = initialize_backend_sentry(_settings())

    assert runtime.redacted_summary() == {
        "enabled": False,
        "initialization_error": "RuntimeError",
    }


def test_confirming_transport_records_http_acknowledgement_and_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = object.__new__(_ConfirmingHttpTransport)
    transport._delivery_results = {}
    transport._delivery_lock = threading.Lock()
    envelope = Envelope()
    envelope.add_event({"event_id": "a" * 32, "message": "test"})
    monkeypatch.setattr(HttpTransport, "_handle_response", lambda *args: None)

    transport._handle_response(SimpleNamespace(status=202), envelope)
    assert transport.delivery_confirmed("a" * 32)

    monkeypatch.setattr(
        HttpTransport,
        "_send_envelope",
        lambda *args: (_ for _ in ()).throw(ConnectionError("network unavailable")),
    )
    with pytest.raises(ConnectionError, match="network unavailable"):
        transport._send_envelope(envelope)
    assert transport.delivery_confirmed("a" * 32) is False


def test_sentry_transport_failure_never_escapes_terminal_capture() -> None:
    initialize_backend_sentry(_settings(), transport=_FailingTransport())

    captured = capture_terminal_exception(
        RuntimeError("worker failed"),
        event="worker_terminal_failure",
        error_code="worker_unhandled_exception",
        outcome="halted",
    )

    assert captured is False


def test_test_event_flush_fails_when_transport_cannot_confirm_delivery() -> None:
    runtime = initialize_backend_sentry(_settings(), transport=_UnconfirmedTransport())

    assert runtime.capture_message(
        "maais_backend_sentry_test_event",
        event="sentry_test_event",
        outcome="qualification",
    )
    assert runtime.flush(timeout=1.0) is False


def test_local_runtime_without_dsn_is_explicitly_disabled() -> None:
    runtime = initialize_backend_sentry(ObservabilitySettings())

    assert runtime.enabled is False
    assert (
        runtime.capture_message(
            "maais_backend_sentry_test_event",
            event="sentry_test_event",
            outcome="test",
        )
        is False
    )
    assert runtime.flush(timeout=1.0) is False
    assert json.loads(json.dumps(runtime.redacted_summary())) == {
        "enabled": False,
        "initialization_error": None,
    }
