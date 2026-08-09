from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID

import pytest

from maais.artifacts.models import ArtifactType
from maais.config.settings import Settings
from maais.operations.cloud_artifacts import (
    _reconcile_cron_delivery_best_effort,
    backup_configured_cloud_database,
    close_configured_cloud_day,
    publish_configured_cloud_bundle,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
REPORT_DATE = date(2026, 8, 9)


class Reporter:
    def __init__(self) -> None:
        self.operations: list[tuple[str, ...]] = []
        self.outcomes: list[str] = []
        self.last_delivery_confirmed = True

    @asynccontextmanager
    async def monitor(self, *operations: str) -> AsyncIterator[None]:
        self.operations.append(operations)
        try:
            yield
        except BaseException:
            self.outcomes.append("error")
            self.last_delivery_confirmed = False
            raise
        self.outcomes.append("ok")
        self.last_delivery_confirmed = True


@pytest.mark.asyncio
async def test_configured_cloud_operations_use_the_exact_cron_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected = object()
    authority = object()
    reconciled: list[tuple[tuple[str, ...], bool]] = []

    async def succeed(**values: object) -> object:
        assert values["runtime_evidence"] is authority
        return expected

    async def operations_evidence(*_: object) -> object:
        return authority

    async def reconcile(**values: object) -> None:
        reporter = values["reporter"]
        reconciled.append(
            (
                values["operations"],  # type: ignore[arg-type]
                reporter.last_delivery_confirmed,  # type: ignore[union-attr]
            )
        )

    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._publish_configured_cloud_bundle_impl",
        succeed,
    )
    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._backup_configured_cloud_database_impl",
        succeed,
    )
    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._close_configured_cloud_day_impl",
        succeed,
    )
    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._operations_evidence",
        operations_evidence,
    )
    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._reconcile_cron_delivery_best_effort",
        reconcile,
    )
    settings = Settings()
    reporter = Reporter()

    assert (
        await publish_configured_cloud_bundle(
            settings=settings,
            run_id=RUN_ID,
            experiment_id=EXPERIMENT_ID,
            report_date=REPORT_DATE,
            artifact_type=ArtifactType.DAILY_REPORT,
            report_id="report-1",
            bundle_directory=tmp_path,
            cron_reporter=reporter,  # type: ignore[arg-type]
        )
        is expected
    )
    assert (
        await backup_configured_cloud_database(
            settings=settings,
            run_id=RUN_ID,
            experiment_id=EXPERIMENT_ID,
            report_date=REPORT_DATE,
            temporary_parent=tmp_path,
            cron_reporter=reporter,  # type: ignore[arg-type]
        )
        is expected
    )
    assert (
        await close_configured_cloud_day(
            settings=settings,
            run_id=RUN_ID,
            experiment_id=EXPERIMENT_ID,
            report_date=REPORT_DATE,
            temporary_parent=tmp_path,
            cron_reporter=reporter,  # type: ignore[arg-type]
        )
        is expected
    )

    assert reporter.operations == [
        ("evidence",),
        ("backup", "evidence"),
        ("daily_close", "backup", "evidence"),
    ]
    assert reporter.outcomes == ["ok", "ok", "ok"]
    assert reconciled == [
        (("evidence",), True),
        (("backup", "evidence"), True),
        (("daily_close", "backup", "evidence"), True),
    ]


@pytest.mark.asyncio
async def test_cron_boundary_never_replaces_the_operation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority = object()
    reconciled: list[bool] = []

    async def fail(**values: object) -> object:
        assert values["runtime_evidence"] is authority
        raise RuntimeError("artifact publication failed")

    async def operations_evidence(*_: object) -> object:
        return authority

    async def reconcile(**values: object) -> None:
        reporter = values["reporter"]
        reconciled.append(reporter.last_delivery_confirmed)  # type: ignore[union-attr]

    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._publish_configured_cloud_bundle_impl",
        fail,
    )
    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._operations_evidence",
        operations_evidence,
    )
    monkeypatch.setattr(
        "maais.operations.cloud_artifacts._reconcile_cron_delivery_best_effort",
        reconcile,
    )
    reporter = Reporter()

    with pytest.raises(RuntimeError, match="artifact publication failed"):
        await publish_configured_cloud_bundle(
            settings=Settings(),
            run_id=RUN_ID,
            experiment_id=EXPERIMENT_ID,
            report_date=REPORT_DATE,
            artifact_type=ArtifactType.DAILY_REPORT,
            report_id="report-1",
            bundle_directory=tmp_path,
            cron_reporter=reporter,  # type: ignore[arg-type]
        )

    assert reporter.operations == [("evidence",)]
    assert reporter.outcomes == ["error"]
    assert reconciled == [False]


@pytest.mark.asyncio
async def test_cron_delivery_persistence_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine(*_: object, **__: object) -> object:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "maais.operations.cloud_artifacts.create_async_engine",
        fail_engine,
    )
    reporter = Reporter()
    reporter.last_delivery_confirmed = False

    await _reconcile_cron_delivery_best_effort(
        settings=Settings(),
        experiment_id=EXPERIMENT_ID,
        operations=("evidence",),
        reporter=reporter,  # type: ignore[arg-type]
    )
