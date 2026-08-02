"""Managed composition of every unauthenticated official paper-data source."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol

from maais.execution.paper.clock import require_utc
from maais.market_data.connectors.binance_rest import BinanceRestConnector
from maais.market_data.connectors.binance_spot import BinanceSpotConnector
from maais.market_data.connectors.binance_websocket import (
    BinanceWebSocketConnector,
    ConnectorHalt,
)
from maais.market_data.connectors.bybit_spot import BybitSpotConnector
from maais.market_data.events import ObservedMarketEvent

Sleep = Callable[[float], Awaitable[None]]
WebSocketFactory = Callable[
    [tuple[str, ...], BinanceRestConnector],
    BinanceWebSocketConnector,
]


class PublicDataRuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class PublicDataFailure:
    reason_code: str
    detail: str
    error_type: str
    detected_at: datetime

    def __post_init__(self) -> None:
        if not self.reason_code or not self.error_type:
            raise ValueError("public data failure identity is required")
        require_utc(self.detected_at, "public data failure detected_at")


class PublicDataHalt(RuntimeError):
    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


class _PublicFuturesPreflight(Protocol):
    venue_clocks: tuple[ObservedMarketEvent, ...]
    symbol_states: tuple[ObservedMarketEvent, ...]


class PublicMarketDataRuntime:
    """Retains all public feed tasks and exposes one bounded loss-intolerant queue."""

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        futures_rest: BinanceRestConnector,
        primary_spot: BinanceSpotConnector,
        secondary_spot: BybitSpotConnector,
        websocket_factory: WebSocketFactory | None = None,
        observed_now: Callable[[], datetime] | None = None,
        sleep: Sleep = asyncio.sleep,
        reference_poll_seconds: float = 1.0,
        preflight_refresh_seconds: float = 30.0,
        queue_size: int = 10_000,
    ) -> None:
        normalized = tuple(symbols)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("public runtime symbols must be nonempty and unique")
        if any(
            not symbol or symbol != symbol.upper() or not symbol.isalnum() for symbol in normalized
        ):
            raise ValueError("public runtime symbols must be uppercase alphanumeric")
        if min(reference_poll_seconds, preflight_refresh_seconds) <= 0 or queue_size <= 0:
            raise ValueError("public runtime intervals and queue capacity must be positive")
        self._symbols = normalized
        self._futures_rest = futures_rest
        self._primary_spot = primary_spot
        self._secondary_spot = secondary_spot
        self._websocket_factory = websocket_factory or (
            lambda configured, rest: BinanceWebSocketConnector(configured, rest=rest)
        )
        self._observed_now = observed_now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._reference_poll_seconds = reference_poll_seconds
        self._preflight_refresh_seconds = preflight_refresh_seconds
        self._queue: asyncio.Queue[ObservedMarketEvent] = asyncio.Queue(maxsize=queue_size)
        self._state = PublicDataRuntimeState.STOPPED
        self._running = False
        self._websocket: BinanceWebSocketConnector | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._failure: PublicDataFailure | None = None

    @property
    def state(self) -> PublicDataRuntimeState:
        return self._state

    @property
    def failure(self) -> PublicDataFailure | None:
        return self._failure

    @property
    def supervisor(self) -> asyncio.Task[None] | None:
        return self._supervisor

    @property
    def websocket(self) -> BinanceWebSocketConnector | None:
        return self._websocket

    async def start(self) -> None:
        if self._state is not PublicDataRuntimeState.STOPPED or self._supervisor is not None:
            raise RuntimeError("public data runtime can be started exactly once")
        self._state = PublicDataRuntimeState.STARTING
        self._running = True
        try:
            futures, _, _ = await asyncio.gather(
                self._futures_rest.preflight(self._symbols),
                self._primary_spot.preflight(self._symbols),
                self._secondary_spot.preflight(self._symbols),
            )
            self._emit_futures_preflight(futures)
            primary, secondary = await asyncio.gather(
                self._primary_spot.get_reference_events(),
                self._secondary_spot.get_reference_events(),
            )
            self._emit_many((*primary, *secondary))
            websocket = self._websocket_factory(self._symbols, self._futures_rest)
            self._websocket = websocket
            await websocket.start()
            self._state = PublicDataRuntimeState.READY
            self._supervisor = asyncio.create_task(
                self._run(),
                name="public_market_data_runtime",
            )
        except Exception as exc:
            await self._halt("public_data_startup_failed", exc)
            raise PublicDataHalt("public_data_startup_failed", _detail(exc)) from exc

    async def stop(self) -> None:
        if self._state is PublicDataRuntimeState.STOPPED:
            return
        halted = self._state is PublicDataRuntimeState.HALTED
        self._running = False
        if not halted:
            self._state = PublicDataRuntimeState.STOPPING
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        if self._websocket is not None:
            await self._websocket.stop()
        if not halted:
            self._state = PublicDataRuntimeState.STOPPED

    async def wait_closed(self) -> None:
        if self._supervisor is not None:
            await self._supervisor

    async def events(self) -> AsyncGenerator[ObservedMarketEvent, None]:
        while True:
            if not self._queue.empty():
                yield self._queue.get_nowait()
                continue
            if self._state is PublicDataRuntimeState.HALTED:
                assert self._failure is not None
                raise PublicDataHalt(
                    self._failure.reason_code,
                    self._failure.detail,
                )
            if self._state is PublicDataRuntimeState.STOPPED and (
                self._supervisor is None or self._supervisor.done()
            ):
                return
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

    async def _run(self) -> None:
        tasks = (
            asyncio.create_task(self._pump_websocket(), name="public_futures_pump"),
            asyncio.create_task(self._poll_references(), name="public_reference_poll"),
            asyncio.create_task(self._refresh_preflight(), name="public_preflight_refresh"),
        )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            if not self._running:
                return
            failure = next(
                (task.exception() for task in done if not task.cancelled()),
                None,
            )
            if failure is None:
                failure = PublicDataHalt(
                    "public_data_task_ended",
                    "a retained public data task ended unexpectedly",
                )
            await self._halt("public_data_task_failed", failure)
        except asyncio.CancelledError:
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump_websocket(self) -> None:
        if self._websocket is None:
            raise RuntimeError("public WebSocket is not initialized")
        async for event in self._websocket.events():
            self._emit(event)
        if self._running:
            raise PublicDataHalt(
                "public_websocket_ended",
                "public futures WebSocket ended while the runtime was active",
            )

    async def _poll_references(self) -> None:
        while self._running:
            await self._sleep(self._reference_poll_seconds)
            if not self._running:
                return
            primary, secondary = await asyncio.gather(
                self._primary_spot.get_reference_events(),
                self._secondary_spot.get_reference_events(),
            )
            self._emit_many((*primary, *secondary))

    async def _refresh_preflight(self) -> None:
        while self._running:
            await self._sleep(self._preflight_refresh_seconds)
            if not self._running:
                return
            futures, _, _ = await asyncio.gather(
                self._futures_rest.preflight(self._symbols),
                self._primary_spot.preflight(self._symbols),
                self._secondary_spot.preflight(self._symbols),
            )
            self._emit_futures_preflight(futures)

    def _emit_futures_preflight(self, preflight: object) -> None:
        clocks = getattr(preflight, "venue_clocks", None)
        states = getattr(preflight, "symbol_states", None)
        if not isinstance(clocks, tuple) or not isinstance(states, tuple):
            raise PublicDataHalt(
                "public_preflight_contract_invalid",
                "futures preflight omitted clock or symbol-state events",
            )
        self._emit_many((*clocks, *states))

    def _emit_many(self, events: Sequence[ObservedMarketEvent]) -> None:
        for event in events:
            self._emit(event)

    def _emit(self, event: ObservedMarketEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise PublicDataHalt(
                "public_runtime_queue_saturated",
                f"public runtime queue saturated before {event.event_id}",
            ) from exc

    async def _halt(self, reason_code: str, exc: BaseException) -> None:
        self._running = False
        detected_at = self._observed_now()
        require_utc(detected_at, "public runtime observed_now")
        detail = _detail(exc)
        if isinstance(exc, ConnectorHalt):
            reason_code = exc.reason_code
            detail = exc.detail
        elif isinstance(exc, PublicDataHalt):
            reason_code = exc.reason_code
            detail = exc.detail
        self._failure = PublicDataFailure(
            reason_code=reason_code,
            detail=detail,
            error_type=type(exc).__name__,
            detected_at=detected_at,
        )
        self._state = PublicDataRuntimeState.HALTED
        if self._websocket is not None:
            await self._websocket.stop()


def _detail(exc: BaseException) -> str:
    detail = str(exc).strip().replace("\x00", "") or "no detail"
    return f"{type(exc).__name__}: {detail}"[:2000]
