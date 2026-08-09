from __future__ import annotations

import json
from pathlib import Path

import pytest

from maais.cli import main
from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.live import PaperLiveConfigurationError
from maais.orchestration.supervisor import PaperWorkerHalt
from tests.unit.experiments.test_runtime_policy import _live_manifest


def test_worker_terminal_error_reports_original_and_halt_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _live_manifest(schema_revision="0015")
    original = ArithmeticError("decision dispatch failed")
    persistence = ConnectionError("database unavailable")
    failure = PaperWorkerHalt.from_terminal_failure(
        original,
        persistence_error=persistence,
    )
    assert isinstance(failure.__cause__, BaseExceptionGroup)
    assert failure.__cause__.exceptions == (original, persistence)
    captured: list[tuple[BaseException, str, str]] = []

    async def fail_worker(*_: object, **__: object) -> None:
        raise failure

    def capture(
        exception: BaseException,
        *,
        event: str,
        error_code: str,
        outcome: str,
        **_: object,
    ) -> bool:
        captured.append((exception, error_code, outcome))
        return True

    monkeypatch.setattr(
        "maais.cli.get_settings",
        lambda: Settings(environment="production", run_mode=RunMode.PAPER_LIVE),
    )
    monkeypatch.setattr("maais.cli.load_manifest_file", lambda _: manifest)
    monkeypatch.setattr("maais.cli.run_live_paper_manifest", fail_worker)
    monkeypatch.setattr("maais.cli.capture_terminal_exception", capture)

    assert main(["paper-live", "--manifest", "candidate.json"]) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert [(item[0], item[1], item[2]) for item in captured] == [
        (original, "worker_unhandled_exception", "halt_persistence_failed"),
        (persistence, "worker_halt_persistence_failed", "halt_persistence_failed"),
    ]
    assert records[-1]["event"] == "paper_live_failed"
    assert records[-1]["error_code"] == "worker_unhandled_exception"
    assert records[-1]["outcome"] == "halt_persistence_failed"


def test_worker_terminal_error_exits_nonzero_when_sentry_capture_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _live_manifest(schema_revision="0015")

    async def fail_worker(*_: object, **__: object) -> None:
        raise RuntimeError("public source retries exhausted")

    def fail_sentry(*_: object, **__: object) -> bool:
        raise RuntimeError("Sentry transport unavailable")

    monkeypatch.setattr(
        "maais.cli.get_settings",
        lambda: Settings(environment="production", run_mode=RunMode.PAPER_LIVE),
    )
    monkeypatch.setattr("maais.cli.load_manifest_file", lambda _: manifest)
    monkeypatch.setattr("maais.cli.run_live_paper_manifest", fail_worker)
    monkeypatch.setattr("maais.cli.capture_terminal_exception", fail_sentry)

    assert main(["paper-live", "--manifest", "candidate.json"]) == 1


def test_expected_worker_configuration_refusal_does_not_create_sentry_noise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _live_manifest(schema_revision="0015")

    async def refuse_worker(*_: object, **__: object) -> None:
        raise PaperLiveConfigurationError("candidate mismatch")

    def unexpected_capture(*_: object, **__: object) -> bool:
        raise AssertionError("expected configuration refusals must not reach Sentry")

    monkeypatch.setattr(
        "maais.cli.get_settings",
        lambda: Settings(environment="production", run_mode=RunMode.PAPER_LIVE),
    )
    monkeypatch.setattr("maais.cli.load_manifest_file", lambda _: manifest)
    monkeypatch.setattr("maais.cli.run_live_paper_manifest", refuse_worker)
    monkeypatch.setattr("maais.cli.capture_terminal_exception", unexpected_capture)

    assert main(["paper-live", "--manifest", "candidate.json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "paper_live_refused"
    assert payload["error_code"] == "worker_configuration_invalid"
    assert "exception" not in payload


def test_sentry_test_event_refuses_an_active_timed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "current.json"
    state.write_text(
        json.dumps(
            {
                "experiment_id": "11111111-1111-4111-8111-111111111111",
                "run_purpose": "soak",
                "started_at": "2026-08-09T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("maais.cli.get_settings", Settings)

    assert main(["sentry-test-event", "--state", str(state)]) == 1


def test_sentry_test_event_requires_capture_and_flush_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Runtime:
        enabled = True
        initialization_error = None

        def capture_message(self, *_: object, **__: object) -> bool:
            return True

        def flush(self, *, timeout: float) -> bool:
            assert timeout == 5.0
            return False

    monkeypatch.setattr("maais.cli.get_settings", Settings)
    monkeypatch.setattr("maais.cli.initialize_backend_sentry", lambda _: Runtime())

    assert main(["sentry-test-event", "--state", str(tmp_path / "missing.json")]) == 1
