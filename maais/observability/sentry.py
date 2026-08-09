"""Privacy-safe Sentry initialization and terminal capture helpers."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import sentry_sdk
from sentry_sdk.crons.api import capture_checkin
from sentry_sdk.crons.consts import MonitorStatus
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import HttpTransport, Transport
from sentry_sdk.types import Event, Hint

from maais.config.observability import ObservabilitySettings
from maais.observability.redaction import redact_value

_FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "request",
        "server_name",
        "user",
    }
)
_current_runtime: SentryRuntime | None = None
_MONITOR_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_CHECK_IN_ID = re.compile(r"[0-9a-f]{32}")
_MONITOR_STATUSES = frozenset({MonitorStatus.IN_PROGRESS, MonitorStatus.OK, MonitorStatus.ERROR})


@dataclass(slots=True)
class SentryRuntime:
    """One process-wide Sentry client without any serialized secret configuration."""

    enabled: bool
    initialization_error: str | None = None
    _configuration_fingerprint: str = ""
    _client: Any | None = None
    _transport: Any | None = None
    _last_event_id: str | None = None

    def capture_exception(
        self,
        exception: BaseException,
        *,
        event: str,
        error_code: str,
        outcome: str,
        tags: Mapping[str, object] | None = None,
        contexts: Mapping[str, object] | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            with sentry_sdk.new_scope() as scope:
                _bind_capture_scope(
                    scope,
                    event=event,
                    error_code=error_code,
                    outcome=outcome,
                    tags=tags,
                    contexts=contexts,
                )
                event_id = sentry_sdk.capture_exception(exception, scope=scope)
        except Exception:
            return False
        self._last_event_id = event_id
        return bool(event_id)

    def capture_message(
        self,
        message: str,
        *,
        event: str,
        outcome: str,
        error_code: str = "",
        tags: Mapping[str, object] | None = None,
        contexts: Mapping[str, object] | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            with sentry_sdk.new_scope() as scope:
                _bind_capture_scope(
                    scope,
                    event=event,
                    error_code=error_code,
                    outcome=outcome,
                    tags=tags,
                    contexts=contexts,
                )
                event_id = sentry_sdk.capture_message(
                    str(redact_value(message)),
                    level="info",
                    scope=scope,
                )
        except Exception:
            return False
        self._last_event_id = event_id
        return bool(event_id)

    def capture_check_in(
        self,
        *,
        monitor_slug: str,
        status: str,
        check_in_id: str | None = None,
        duration: float | None = None,
    ) -> str | None:
        if (
            not self.enabled
            or _MONITOR_SLUG.fullmatch(monitor_slug) is None
            or status not in _MONITOR_STATUSES
            or (check_in_id is not None and _CHECK_IN_ID.fullmatch(check_in_id) is None)
            or (duration is not None and duration < 0)
        ):
            return None
        try:
            captured_id = capture_checkin(
                monitor_slug=monitor_slug,
                check_in_id=check_in_id,
                status=status,
                duration=duration,
            )
        except Exception:
            return None
        self._last_event_id = captured_id
        return captured_id

    def flush(self, *, timeout: float = 5.0) -> bool:
        if not self.enabled:
            return False
        try:
            sentry_sdk.flush(timeout=timeout)
        except Exception:
            return False
        transport = self._transport
        if (
            self._last_event_id is not None
            and transport is not None
            and hasattr(transport, "delivery_confirmed")
        ):
            return bool(transport.delivery_confirmed(self._last_event_id))
        return True

    def redacted_summary(self) -> dict[str, bool | str | None]:
        return {
            "enabled": self.enabled,
            "initialization_error": self.initialization_error,
        }


def initialize_backend_sentry(
    settings: ObservabilitySettings,
    *,
    transport: Transport | type[Transport] | None = None,
) -> SentryRuntime:
    """Initialize the one backend client, or return an explicit disabled runtime."""
    global _current_runtime

    dsn = settings.backend_dsn_value
    if not dsn:
        return SentryRuntime(enabled=False)
    fingerprint = _configuration_fingerprint(settings, dsn=dsn, transport=transport)
    if _current_runtime is not None:
        if _current_runtime._configuration_fingerprint != fingerprint:
            return SentryRuntime(
                enabled=False,
                initialization_error="configuration_conflict",
            )
        return _current_runtime

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.environment,
            release=settings.release,
            sample_rate=1.0,
            send_default_pii=False,
            traces_sample_rate=settings.traces_sample_rate,
            profiles_sample_rate=settings.profiles_sample_rate,
            max_request_body_size="never",
            include_local_variables=False,
            include_source_context=False,
            auto_session_tracking=False,
            enable_logs=False,
            send_client_reports=False,
            trace_propagation_targets=[],
            server_name="",
            before_send=redact_sentry_event,
            before_breadcrumb=redact_sentry_breadcrumb,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                LoggingIntegration(level=None, event_level=None, sentry_logs_level=None),
            ],
            transport=transport or _ConfirmingHttpTransport,
        )
    except Exception as exc:
        return SentryRuntime(
            enabled=False,
            initialization_error=type(exc).__name__,
        )

    client = sentry_sdk.get_client()
    sentry_sdk.get_global_scope().set_tag(
        "maais.service_role",
        settings.service_role.value if settings.service_role is not None else "local",
    )
    sentry_sdk.get_global_scope().set_tag(
        "maais.deployment_target",
        settings.deployment_target.value,
    )
    _current_runtime = SentryRuntime(
        enabled=True,
        _configuration_fingerprint=fingerprint,
        _client=client,
        _transport=client.transport,
    )
    return _current_runtime


def capture_terminal_exception(
    exception: BaseException,
    *,
    event: str,
    error_code: str,
    outcome: str,
    tags: Mapping[str, object] | None = None,
    contexts: Mapping[str, object] | None = None,
) -> bool:
    runtime = _current_runtime
    if runtime is None:
        return False
    return runtime.capture_exception(
        exception,
        event=event,
        error_code=error_code,
        outcome=outcome,
        tags=tags,
        contexts=contexts,
    )


def flush_backend_sentry(*, timeout: float = 5.0) -> bool:
    runtime = _current_runtime
    return runtime.flush(timeout=timeout) if runtime is not None else False


def shutdown_backend_sentry() -> None:
    """Release the active client; intended for deterministic process/test shutdown."""
    global _current_runtime

    runtime = _current_runtime
    _current_runtime = None
    if runtime is None:
        return
    client = runtime._client
    try:
        if client is not None:
            client.close(timeout=1.0)
    except Exception:
        pass
    for scope in (
        sentry_sdk.get_current_scope(),
        sentry_sdk.get_isolation_scope(),
        sentry_sdk.get_global_scope(),
    ):
        scope.clear()


def redact_sentry_event(event: Event, hint: Hint) -> Event | None:
    """Apply the complete off-platform privacy boundary before transport."""
    del hint
    sanitized = dict(event)
    for field in _FORBIDDEN_EVENT_FIELDS:
        sanitized.pop(field, None)
    redacted = redact_value(sanitized)
    return cast(Event, redacted) if isinstance(redacted, dict) else None


def redact_sentry_breadcrumb(breadcrumb: dict[str, Any], hint: Hint) -> dict[str, Any] | None:
    del hint
    redacted = redact_value(breadcrumb)
    return cast(dict[str, Any], redacted) if isinstance(redacted, dict) else None


def _configuration_fingerprint(
    settings: ObservabilitySettings,
    *,
    dsn: str,
    transport: Transport | type[Transport] | None,
) -> str:
    values = (
        settings.environment,
        settings.release,
        settings.deployment_target.value,
        settings.service_role.value if settings.service_role is not None else "local",
        str(settings.traces_sample_rate),
        str(settings.profiles_sample_rate),
        hashlib.sha256(dsn.encode("utf-8")).hexdigest(),
        str(id(transport)) if transport is not None else "default",
    )
    return hashlib.sha256("\x00".join(values).encode("utf-8")).hexdigest()


def _bind_capture_scope(
    scope: Any,
    *,
    event: str,
    error_code: str,
    outcome: str,
    tags: Mapping[str, object] | None,
    contexts: Mapping[str, object] | None,
) -> None:
    scope.set_tag("maais.event", str(redact_value(event)))
    scope.set_tag("maais.outcome", str(redact_value(outcome)))
    if error_code:
        scope.set_tag("maais.error_code", str(redact_value(error_code)))
    for key, value in (tags or {}).items():
        scope.set_tag(f"maais.{key}", str(redact_value(value, key=str(key))))
    scope.set_context(
        "maais",
        cast(
            dict[str, object],
            redact_value(
                {
                    "event": event,
                    "error_code": error_code,
                    "outcome": outcome,
                    **dict(contexts or {}),
                }
            ),
        ),
    )
    scope.fingerprint = [event, error_code or outcome]


class _ConfirmingHttpTransport(HttpTransport):
    """Record whether Sentry acknowledged each envelope with a 2xx response."""

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self._delivery_results: dict[str, bool] = {}
        self._delivery_lock = threading.Lock()

    def _send_envelope(self, envelope: Any) -> None:
        try:
            super()._send_envelope(envelope)
        except Exception:
            self._record_delivery(envelope, delivered=False)
            raise

    def _handle_response(self, response: Any, envelope: Any) -> None:
        super()._handle_response(response, envelope)
        self._record_delivery(
            envelope,
            delivered=200 <= int(response.status) < 300,
        )

    def delivery_confirmed(self, event_id: str) -> bool:
        with self._delivery_lock:
            return self._delivery_results.get(event_id, False)

    def _record_delivery(self, envelope: Any, *, delivered: bool) -> None:
        delivery_id = _delivery_identity(envelope)
        if delivery_id is not None:
            with self._delivery_lock:
                self._delivery_results[delivery_id] = delivered


def _delivery_identity(envelope: Any) -> str | None:
    if envelope is None:
        return None
    event = envelope.get_event()
    event_id = event.get("event_id") if isinstance(event, dict) else None
    if isinstance(event_id, str):
        return event_id
    for item in envelope:
        if getattr(item, "type", None) != "check_in":
            continue
        payload = getattr(getattr(item, "payload", None), "json", None)
        check_in_id = payload.get("check_in_id") if isinstance(payload, dict) else None
        if isinstance(check_in_id, str):
            return check_in_id
    return None
