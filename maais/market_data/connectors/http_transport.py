"""Bounded recovery for transient public REST transport failures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

import httpx

from maais.core.logging import get_logger

logger = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
QueryValue = str | int | float


async def get_with_transport_retry(
    client: httpx.AsyncClient,
    path: str,
    params: Mapping[str, QueryValue],
    *,
    component: str,
    sleep: Sleep,
    max_attempts: int = 3,
    initial_backoff_seconds: float = 0.25,
) -> httpx.Response:
    """Retry only connection/transport failures and preserve fail-closed contracts."""

    if max_attempts <= 0 or initial_backoff_seconds < 0:
        raise ValueError("transport retry attempts must be positive and backoff nonnegative")
    for attempt in range(1, max_attempts + 1):
        try:
            return await client.get(path, params=params)
        except httpx.TransportError as exc:
            fields = {
                "component": component,
                "path": path,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            if attempt == max_attempts:
                logger.error("public_rest_transport_exhausted", **fields)
                raise
            delay = initial_backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "public_rest_transport_retry",
                **fields,
                retry_in_seconds=delay,
            )
            await sleep(delay)
    raise AssertionError("unreachable transport retry state")
