"""End-to-end tests for the structured logging processor chain."""

from __future__ import annotations

import json
import logging
from uuid import UUID

import pytest

from maais.core.logging import configure_logging, get_logger
from maais.observability.context import bind_log_context, bind_telemetry_context
from maais.observability.events import EVENT_SCHEMA_VERSION, TelemetryContext
from tests.unit.observability.test_redaction import CANARIES, assert_canaries_absent

TELEMETRY = TelemetryContext(
    service_role="worker",
    environment="qualification",
    release="a" * 40,
    candidate_hash="b" * 64,
    deployment_id="deployment-1",
    replica_id="replica-1",
    region="europe-west4-drams3a",
    boot_id=UUID("11111111-1111-4111-8111-111111111111"),
)


@pytest.fixture(autouse=True)
def restore_test_logging() -> None:
    yield
    configure_logging(log_level="WARNING", is_production=False)


def _json_lines(captured: str) -> list[dict[str, object]]:
    lines = [line for line in captured.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_production_logging_emits_one_versioned_allowlisted_json_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", is_production=True)

    with (
        bind_telemetry_context(TELEMETRY),
        bind_log_context(
            correlation_id="correlation-1",
            operation_id="operation-1",
            experiment_ref="experiment-ref-1",
            decision_cycle_id="decision-cycle-1",
        ),
    ):
        get_logger("maais.test").info(
            "paper_cycle_completed",
            symbol="BTCUSDT",
            outcome="neutral",
            duration_ms=12.5,
            retry_count=0,
            reason_code="no_consensus",
            unknown_field="must-be-dropped",
            account_equity="must-be-dropped",
        )

    events = _json_lines(capsys.readouterr().out)

    assert len(events) == 1
    assert events[0] == {
        "boot_id": "11111111-1111-4111-8111-111111111111",
        "candidate_hash": "b" * 64,
        "correlation_id": "correlation-1",
        "decision_cycle_id": "decision-cycle-1",
        "deployment_id": "deployment-1",
        "duration_ms": 12.5,
        "environment": "qualification",
        "event": "paper_cycle_completed",
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "experiment_ref": "experiment-ref-1",
        "level": "info",
        "logger": "maais.test",
        "operation_id": "operation-1",
        "outcome": "neutral",
        "reason_code": "no_consensus",
        "region": "europe-west4-drams3a",
        "release": "a" * 40,
        "replica_id": "replica-1",
        "retry_count": 0,
        "service_role": "worker",
        "symbol": "BTCUSDT",
        "timestamp": events[0]["timestamp"],
    }
    assert str(events[0]["timestamp"]).endswith("Z")


def test_standard_library_logs_use_the_same_redaction_and_json_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", is_production=True)

    logging.getLogger("foreign.library").error(
        "request failed for %s",
        CANARIES[0],
        extra={
            "operation_id": "operation-2",
            "authorization": CANARIES[1],
            "unknown": CANARIES[2],
        },
    )
    event = _json_lines(capsys.readouterr().out)[0]
    serialized = json.dumps(event, sort_keys=True)

    assert event["logger"] == "foreign.library"
    assert event["level"] == "error"
    assert event["operation_id"] == "operation-2"
    assert "authorization" not in event
    assert "unknown" not in event
    assert_canaries_absent(serialized)


def test_console_logging_applies_redaction_and_allowlist_before_rendering(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", is_production=False)

    get_logger("maais.console").warning(
        "public_rest_transport_retry",
        reason_code=f"failed-{CANARIES[3]}",
        unknown=CANARIES[4],
    )
    rendered = capsys.readouterr().out

    assert "public_rest_transport_retry" in rendered
    assert "unknown" not in rendered
    assert_canaries_absent(rendered)


def test_bound_context_is_serialized_and_always_cleared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", is_production=True)

    with pytest.raises(RuntimeError, match="expected failure"):
        with bind_telemetry_context(TELEMETRY), bind_log_context(correlation_id="temporary"):
            get_logger("maais.context").info("inside")
            raise RuntimeError("expected failure")
    get_logger("maais.context").info("outside")

    inside, outside = _json_lines(capsys.readouterr().out)
    assert inside["correlation_id"] == "temporary"
    assert inside["service_role"] == "worker"
    assert "correlation_id" not in outside
    assert "service_role" not in outside


def test_exception_chain_is_bounded_structured_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(log_level="INFO", is_production=True)

    try:
        try:
            raise ValueError(f"database failed: {CANARIES[0]}")
        except ValueError as cause:
            raise RuntimeError(f"wrapper failed: {CANARIES[5]}") from cause
    except RuntimeError:
        get_logger("maais.failure").exception(
            "worker_terminal_failure",
            error_code="worker_unhandled_exception",
        )

    event = _json_lines(capsys.readouterr().out)[0]
    exception = event["exception"]
    assert isinstance(exception, dict)
    assert exception["type"] == "RuntimeError"
    assert exception["causes"][0]["type"] == "ValueError"
    assert "test_exception_chain_is_bounded_structured_and_redacted" in exception["stack"]
    assert_canaries_absent(json.dumps(event, sort_keys=True))


def test_console_exception_logging_is_visible_redacted_and_warning_free(
    capsys: pytest.CaptureFixture[str],
    recwarn: pytest.WarningsRecorder,
) -> None:
    configure_logging(log_level="INFO", is_production=False)

    try:
        raise RuntimeError(f"console failed: {CANARIES[2]}")
    except RuntimeError:
        logging.getLogger("foreign.console").error(
            "console_terminal_failure",
            exc_info=True,
        )

    rendered = capsys.readouterr().out
    assert "console_terminal_failure" in rendered
    assert "RuntimeError" in rendered
    assert_canaries_absent(rendered)
    assert not recwarn.list


def test_unrenderable_exception_cannot_break_terminal_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnrenderableError(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("cannot render exception")

    configure_logging(log_level="INFO", is_production=True)

    try:
        raise UnrenderableError()
    except UnrenderableError:
        get_logger("maais.failure").exception(
            "worker_terminal_failure",
            error_code="worker_unhandled_exception",
        )

    event = _json_lines(capsys.readouterr().out)[0]
    assert event["exception"]["type"] == "UnrenderableError"
    assert event["exception"]["message"] == "[UNSERIALIZABLE]"
