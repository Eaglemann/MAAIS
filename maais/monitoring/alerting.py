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

import httpx

from maais.core.logging import get_logger
from maais.monitoring.schemas import AlertEvent, AlertLevel

logger = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


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
        await self.send(AlertEvent(
            level=AlertLevel.CRITICAL,
            component=component,
            title=title,
            message=message,
            metadata=meta,
        ))

    async def send_warning(self, component: str, title: str, message: str, **meta) -> None:
        """Convenience method for WARNING alerts."""
        await self.send(AlertEvent(
            level=AlertLevel.WARNING,
            component=component,
            title=title,
            message=message,
            metadata=meta,
        ))

    async def send_info(self, component: str, title: str, message: str, **meta) -> None:
        """Convenience method for INFO alerts."""
        await self.send(AlertEvent(
            level=AlertLevel.INFO,
            component=component,
            title=title,
            message=message,
            metadata=meta,
        ))

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
