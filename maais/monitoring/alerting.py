"""Alert dispatcher — structlog + Telegram (Rule 17/18).

Two channels:
  1. structlog — always; JSON in prod, console in dev.
  2. Telegram   — when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in .env.

Telegram message format:
  [LEVEL] TITLE
  Component: component_name
  Message: detail text
  Time: ISO timestamp
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Protocol

import httpx

from maais.core.logging import get_logger
from maais.monitoring.schemas import AlertEvent, AlertLevel

logger = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_CRON_OPERATION_NAMES = frozenset({"daily_close", "backup", "evidence"})


class SentryCheckInRuntime(Protocol):
    enabled: bool

    def capture_check_in(
        self,
        *,
        monitor_slug: str,
        status: str,
        check_in_id: str | None = None,
        duration: float | None = None,
    ) -> str | None: ...

    def flush(self, *, timeout: float = 5.0) -> bool: ...


class SentryCronReporter:
    """Report Cron outcomes without becoming authority for the operation result."""

    def __init__(
        self,
        *,
        runtime: SentryCheckInRuntime,
        monitor_slugs: Mapping[str, str],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not set(monitor_slugs) <= _CRON_OPERATION_NAMES:
            raise ValueError("Sentry Cron operation name is invalid")
        if any(not slug for slug in monitor_slugs.values()):
            raise ValueError("Sentry Cron monitor slug cannot be empty")
        self._runtime = runtime
        self._monitor_slugs = dict(monitor_slugs)
        self._monotonic = monotonic
        self.last_delivery_confirmed = bool(runtime.enabled and monitor_slugs)

    @asynccontextmanager
    async def monitor(self, *operations: str) -> AsyncIterator[None]:
        if not operations or len(set(operations)) != len(operations):
            raise ValueError("Sentry Cron operations must be nonempty and unique")
        missing = set(operations) - set(self._monitor_slugs)
        if missing:
            raise ValueError("Sentry Cron monitor slug is not configured")
        started_at = self._monotonic()
        deliveries: list[bool] = []
        check_in_ids: dict[str, str | None] = {}
        for operation in operations:
            check_in_id, delivered = self._send(
                monitor_slug=self._monitor_slugs[operation],
                status="in_progress",
            )
            check_in_ids[operation] = check_in_id
            deliveries.append(delivered)
        try:
            yield
        except BaseException:
            duration = max(0.0, self._monotonic() - started_at)
            for operation in operations:
                _, delivered = self._send(
                    monitor_slug=self._monitor_slugs[operation],
                    status="error",
                    check_in_id=check_in_ids[operation],
                    duration=duration,
                )
                deliveries.append(delivered)
            self.last_delivery_confirmed = all(deliveries)
            raise
        duration = max(0.0, self._monotonic() - started_at)
        for operation in operations:
            _, delivered = self._send(
                monitor_slug=self._monitor_slugs[operation],
                status="ok",
                check_in_id=check_in_ids[operation],
                duration=duration,
            )
            deliveries.append(delivered)
        self.last_delivery_confirmed = all(deliveries)

    def _send(
        self,
        *,
        monitor_slug: str,
        status: str,
        check_in_id: str | None = None,
        duration: float | None = None,
    ) -> tuple[str | None, bool]:
        try:
            captured_id = self._runtime.capture_check_in(
                monitor_slug=monitor_slug,
                status=status,
                check_in_id=check_in_id,
                duration=duration,
            )
            return captured_id, bool(captured_id and self._runtime.flush(timeout=5.0))
        except Exception:
            return None, False


class AlertDispatcher:
    """Dispatches alerts to structlog and optionally Telegram."""

    def __init__(
        self,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        self._token = telegram_token
        self._chat_id = telegram_chat_id
        self._telegram_enabled = bool(telegram_token and telegram_chat_id)

    async def send(self, event: AlertEvent) -> None:
        """Dispatch an alert to all configured channels."""
        _log_alert(event)
        if self._telegram_enabled:
            await self._send_telegram(event)

    async def send_critical(self, component: str, title: str, message: str, **meta) -> None:
        """Convenience method for CRITICAL alerts."""
        await self.send(
            AlertEvent(
                level=AlertLevel.CRITICAL,
                component=component,
                title=title,
                message=message,
                metadata=meta,
            )
        )

    async def send_warning(self, component: str, title: str, message: str, **meta) -> None:
        """Convenience method for WARNING alerts."""
        await self.send(
            AlertEvent(
                level=AlertLevel.WARNING,
                component=component,
                title=title,
                message=message,
                metadata=meta,
            )
        )

    async def send_info(self, component: str, title: str, message: str, **meta) -> None:
        """Convenience method for INFO alerts."""
        await self.send(
            AlertEvent(
                level=AlertLevel.INFO,
                component=component,
                title=title,
                message=message,
                metadata=meta,
            )
        )

    async def _send_telegram(self, event: AlertEvent) -> None:
        text = (
            f"[{event.level.value}] {event.title}\n"
            f"Component: {event.component}\n"
            f"Message: {event.message}\n"
            f"Time: {event.timestamp.isoformat()}"
        )
        url = _TELEGRAM_API.format(token=self._token)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json={"chat_id": self._chat_id, "text": text})
                resp.raise_for_status()
        except Exception as exc:
            logger.error("telegram_alert_failed", error=str(exc), title=event.title)

    @property
    def telegram_enabled(self) -> bool:
        return self._telegram_enabled


def _log_alert(event: AlertEvent) -> None:
    log_fn = {
        AlertLevel.INFO: logger.info,
        AlertLevel.WARNING: logger.warning,
        AlertLevel.CRITICAL: logger.error,
    }.get(event.level, logger.info)
    log_fn(
        "alert",
        level=event.level.value,
        component=event.component,
        title=event.title,
        message=event.message,
        **event.metadata,
    )
