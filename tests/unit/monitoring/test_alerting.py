from __future__ import annotations

from collections.abc import Iterator

import pytest

from maais.monitoring.alerting import SentryCronReporter


class Runtime:
    def __init__(self, *, enabled: bool = True, flush_result: bool = True) -> None:
        self.enabled = enabled
        self.flush_result = flush_result
        self.calls: list[dict[str, object]] = []

    def capture_check_in(
        self,
        *,
        monitor_slug: str,
        status: str,
        check_in_id: str | None = None,
        duration: float | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "monitor_slug": monitor_slug,
                "status": status,
                "check_in_id": check_in_id,
                "duration": duration,
            }
        )
        return check_in_id or f"check-{len(self.calls)}" if self.enabled else None

    def flush(self, *, timeout: float = 5.0) -> bool:
        assert timeout == 5.0
        return self.flush_result


@pytest.mark.asyncio
async def test_cron_reporter_sends_started_and_success_for_each_operation() -> None:
    runtime = Runtime()
    elapsed = iter((10.0, 12.5))
    reporter = SentryCronReporter(
        runtime=runtime,
        monitor_slugs={
            "daily_close": "daily-monitor",
            "backup": "backup-monitor",
            "evidence": "evidence-monitor",
        },
        monotonic=lambda: next(elapsed),
    )

    async with reporter.monitor("daily_close", "backup", "evidence"):
        pass

    assert [call["status"] for call in runtime.calls] == [
        "in_progress",
        "in_progress",
        "in_progress",
        "ok",
        "ok",
        "ok",
    ]
    assert [call["duration"] for call in runtime.calls[-3:]] == [2.5, 2.5, 2.5]
    assert reporter.last_delivery_confirmed is True


@pytest.mark.asyncio
async def test_cron_reporter_preserves_operation_failure_and_sentry_outage_is_local() -> None:
    runtime = Runtime(flush_result=False)
    elapsed: Iterator[float] = iter((5.0, 6.0))
    reporter = SentryCronReporter(
        runtime=runtime,
        monitor_slugs={"backup": "backup-monitor"},
        monotonic=lambda: next(elapsed),
    )

    with pytest.raises(RuntimeError, match="backup failed"):
        async with reporter.monitor("backup"):
            raise RuntimeError("backup failed")

    assert [call["status"] for call in runtime.calls] == ["in_progress", "error"]
    assert reporter.last_delivery_confirmed is False


@pytest.mark.asyncio
async def test_cron_reporter_never_suppresses_success_when_sentry_capture_raises() -> None:
    class RaisingRuntime(Runtime):
        def capture_check_in(self, **_: object) -> str | None:
            raise ConnectionError("sentry unavailable")

    reporter = SentryCronReporter(
        runtime=RaisingRuntime(),
        monitor_slugs={"evidence": "evidence-monitor"},
    )

    async with reporter.monitor("evidence"):
        completed = True

    assert completed is True
    assert reporter.last_delivery_confirmed is False
