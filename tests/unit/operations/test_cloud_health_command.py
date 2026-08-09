from __future__ import annotations

from uuid import UUID

import pytest

from maais.cli import _validate_cloud_operations_settings, build_parser, main
from maais.config.cloud import ServiceRole
from maais.config.settings import Settings
from tests.unit.config.test_cloud_settings import _railway_settings

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_cloud_operations_requires_the_complete_operations_runtime_contract() -> None:
    with pytest.raises(ValueError, match="Railway"):
        _validate_cloud_operations_settings(Settings())

    worker = _railway_settings()
    with pytest.raises(ValueError, match="operations service role"):
        _validate_cloud_operations_settings(worker)

    operations = _railway_settings(
        service_role=ServiceRole.OPERATIONS,
        database_role_name="maais_ops",
        railway_service_id="operations-service",
    )
    _validate_cloud_operations_settings(operations)


def test_cloud_operations_parser_requires_the_exact_run_identity() -> None:
    arguments = build_parser().parse_args(["cloud-operations", "--run", str(RUN_ID)])

    assert arguments.command == "cloud-operations"
    assert arguments.run == RUN_ID


def test_cloud_operations_command_runs_until_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[UUID] = []

    async def run_cloud_operations(
        *, settings: Settings, run_id: UUID, sentry_runtime: object
    ) -> None:
        assert isinstance(settings, Settings)
        assert sentry_runtime is not None
        calls.append(run_id)

    monkeypatch.setattr("maais.cli.get_settings", Settings)
    monkeypatch.setattr("maais.cli.run_cloud_operations", run_cloud_operations)

    assert main(["cloud-operations", "--run", str(RUN_ID)]) == 0
    assert calls == [RUN_ID]


def test_cloud_operations_terminal_failure_returns_nonzero_without_hiding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(**_: object) -> None:
        raise RuntimeError("health supervisor failed")

    captures: list[str] = []
    monkeypatch.setattr("maais.cli.get_settings", Settings)
    monkeypatch.setattr("maais.cli.run_cloud_operations", fail)
    monkeypatch.setattr(
        "maais.cli._capture_exception_without_suppressing_exit",
        lambda _error, **values: captures.append(str(values["error_code"])),
    )

    assert main(["cloud-operations", "--run", str(RUN_ID)]) == 1
    assert captures == ["cloud_operations_unhandled_exception"]
