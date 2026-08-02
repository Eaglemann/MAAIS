"""Managed keyless Binance USD-M WebSocket connector for official paper inputs."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

import websockets
from websockets.exceptions import ConnectionClosed

from maais.execution.paper.clock import require_utc
from maais.market_data.connectors.binance_contracts import (
    BinanceContractError,
    BinanceDepthBook,
    BinanceDepthDelta,
    BinanceDepthSnapshot,
    BinanceSequenceGap,
    parse_websocket_message,
)
from maais.market_data.events import ObservedMarketEvent

PUBLIC_FSTREAM_BASE_URL = "wss://fstream.binance.com/stream"
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0
_STABLE_CONNECTION_SECONDS = 30.0
_DEFAULT_QUEUE_SIZE = 10_000
_DEFAULT_DEPTH_BUFFER_SIZE = 2_000
Sleep = Callable[[float], Awaitable[None]]
ConnectFactory = Callable[[str], Any]


class _RestDepthSource(Protocol):
    @property
    def preflight_complete(self) -> bool: ...

    @property
    def preflight_result(self) -> object: ...

    async def get_depth_snapshot(self, symbol: str) -> BinanceDepthSnapshot: ...


class _Socket(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def close(self) -> None: ...


class ConnectorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    RECOVERING = "recovering"
    STOPPING = "stopping"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class ConnectorFailure:
    reason_code: str
    detail: str
    error_type: str
    detected_at: datetime
    requires_operator_review: bool

    def __post_init__(self) -> None:
        if not self.reason_code or not self.error_type:
            raise ValueError("connector failure identity is required")
        require_utc(self.detected_at, "connector failure detected_at")


class ConnectorHalt(RuntimeError):
    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


class _RecoverableDisconnect(ConnectionError):
    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


def _build_stream_url(symbols: tuple[str, ...] | list[str]) -> str:
    streams: list[str] = []
    for symbol in symbols:
        stream_symbol = symbol.lower()
        streams.extend(
            (
                f"{stream_symbol}@kline_1m",
                f"{stream_symbol}@depth@500ms",
                f"{stream_symbol}@aggTrade",
                f"{stream_symbol}@markPrice@1s",
            )
        )
    return f"{PUBLIC_FSTREAM_BASE_URL}?streams={'/'.join(streams)}"


def _default_connect(url: str):
    return websockets.connect(
        url,
        ping_interval=20,
        ping_timeout=10,
        close_timeout=5,
        max_queue=1024,
    )


class BinanceWebSocketConnector:
    """Retained-task public stream with strict contracts and lossless backpressure."""

    def __init__(
        self,
        symbols: tuple[str, ...] | list[str],
        *,
        rest: _RestDepthSource,
        connect: ConnectFactory = _default_connect,
        observed_now: Callable[[], datetime] | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[float, float], float] | None = None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
        depth_buffer_size: int = _DEFAULT_DEPTH_BUFFER_SIZE,
        published_depth: int = 20,
        ready_timeout: float = 30.0,
    ) -> None:
        normalized = tuple(symbols)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("connector symbols must be nonempty and unique")
        if any(
            not symbol or symbol != symbol.upper() or not symbol.isalnum()
            for symbol in normalized
        ):
            raise ValueError("connector symbols must be uppercase alphanumeric")
        if min(queue_size, depth_buffer_size, published_depth) <= 0 or ready_timeout <= 0:
            raise ValueError("connector capacities and ready timeout must be positive")
        if not rest.preflight_complete:
            raise RuntimeError("Binance REST preflight must complete before WebSocket setup")
        admitted = {
            item.symbol
            for item in getattr(rest.preflight_result, "exchange_filters", ())
        }
        missing = sorted(set(normalized) - admitted)
        if missing:
            raise RuntimeError(f"WebSocket symbols were not admitted by preflight: {missing}")

        self._symbols = normalized
        self._url = _build_stream_url(normalized)
        self._rest = rest
        self._connect = connect
        self._observed_now = observed_now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._jitter = jitter or random.SystemRandom().uniform
        self._ready_timeout = ready_timeout
        self._published_depth = published_depth
        self._depth_buffer_size = depth_buffer_size
        self._queue: asyncio.Queue[ObservedMarketEvent] = asyncio.Queue(maxsize=queue_size)
        self._state = ConnectorState.STOPPED
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._socket: _Socket | None = None
        self._ready = asyncio.Event()
        self._books: dict[str, BinanceDepthBook] = {}
        self._depth_buffers: dict[str, list[BinanceDepthDelta]] = {}
        self._book_lock = asyncio.Lock()
        self._failure: ConnectorFailure | None = None
        self._recovery_count = 0
        self._last_recovery_reason: str | None = None
        self._last_recovery_detail: str | None = None

    @property
    def state(self) -> ConnectorState:
        return self._state

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def failure(self) -> ConnectorFailure | None:
        return self._failure

    @property
    def recovery_count(self) -> int:
        return self._recovery_count

    @property
    def last_recovery_reason(self) -> str | None:
        return self._last_recovery_reason

    @property
    def last_recovery_detail(self) -> str | None:
        return self._last_recovery_detail

    async def start(self) -> None:
        if self._state is not ConnectorState.STOPPED or self._task is not None:
            raise RuntimeError("connector can be started exactly once")
        self._running = True
        self._state = ConnectorState.STARTING
        self._task = asyncio.create_task(self._run(), name="binance_public_websocket")
        ready_wait = asyncio.create_task(self._ready.wait())
        done, _ = await asyncio.wait(
            {ready_wait, self._task},
            timeout=self._ready_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ready_wait in done and ready_wait.result():
            return
        ready_wait.cancel()
        await asyncio.gather(ready_wait, return_exceptions=True)
        if self._task in done:
            if self._failure is not None:
                raise ConnectorHalt(self._failure.reason_code, self._failure.detail)
            raise ConnectorHalt("startup_ended", "connector task ended before readiness")
        await self._halt_from_outside(
            ConnectorHalt("ready_timeout", "connector did not become ready before timeout")
        )
        raise ConnectorHalt("ready_timeout", "connector did not become ready before timeout")

    async def stop(self) -> None:
        if self._state is ConnectorState.STOPPED:
            return
        halted = self._state is ConnectorState.HALTED
        self._running = False
        if not halted:
            self._state = ConnectorState.STOPPING
        if self._socket is not None:
            await self._socket.close()
        if self._task is not None and not self._task.done():
            await self._task
        if not halted:
            self._state = ConnectorState.STOPPED

    async def wait_closed(self, *, timeout: float | None = None) -> None:
        if self._task is None:
            return
        if timeout is None:
            await self._task
        else:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)

    async def events(self) -> AsyncGenerator[ObservedMarketEvent, None]:
        while True:
            if not self._queue.empty():
                yield self._queue.get_nowait()
                continue
            if self._state is ConnectorState.HALTED:
                assert self._failure is not None
                raise ConnectorHalt(self._failure.reason_code, self._failure.detail)
            if self._state is ConnectorState.STOPPED and (
                self._task is None or self._task.done()
            ):
                return
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

    async def _run(self) -> None:
        backoff = _BACKOFF_BASE
        try:
            while self._running:
                connected_at = asyncio.get_running_loop().time()
                try:
                    async with self._connect(self._url) as socket:
                        self._socket = socket
                        await self._run_session(socket)
                except ConnectorHalt as exc:
                    self._set_halt(exc)
                    return
                except BinanceContractError as exc:
                    self._set_halt(ConnectorHalt("public_contract_violation", str(exc)))
                    return
                except asyncio.CancelledError:
                    raise
                except BinanceSequenceGap as exc:
                    reason = "depth_sequence_gap"
                    detail = str(exc)
                except ConnectionClosed as exc:
                    reason = "websocket_connection_closed"
                    detail = f"code={exc.code} reason={exc.reason}"
                except _RecoverableDisconnect as exc:
                    reason = exc.reason_code
                    detail = str(exc)
                except Exception as exc:
                    reason = "websocket_connection_error"
                    detail = f"{type(exc).__name__}: {exc}"
                finally:
                    self._socket = None

                if not self._running:
                    break
                self._recovery_count += 1
                self._last_recovery_reason = reason
                self._last_recovery_detail = detail
                self._state = ConnectorState.RECOVERING
                if asyncio.get_running_loop().time() - connected_at >= _STABLE_CONNECTION_SECONDS:
                    backoff = _BACKOFF_BASE
                wait = backoff + self._jitter(0, backoff * 0.2)
                await self._sleep(wait)
                backoff = min(backoff * 2, _BACKOFF_MAX)
        finally:
            if self._state is not ConnectorState.HALTED:
                self._state = ConnectorState.STOPPED

    async def _run_session(self, socket: _Socket) -> None:
        async with self._book_lock:
            self._books = {}
            self._depth_buffers = {symbol: [] for symbol in self._symbols}
        initializer = asyncio.create_task(self._initialize_books(), name="binance_depth_snapshot")
        reader = asyncio.create_task(self._read_messages(socket), name="binance_stream_reader")
        try:
            done, _ = await asyncio.wait(
                {initializer, reader},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if reader in done:
                await reader
                raise _RecoverableDisconnect(
                    "websocket_stream_ended",
                    "public websocket stream ended before stop",
                )
            await initializer
            await reader
            raise _RecoverableDisconnect(
                "websocket_stream_ended",
                "public websocket stream ended before stop",
            )
        finally:
            for task in (initializer, reader):
                if not task.done():
                    task.cancel()
            await asyncio.gather(initializer, reader, return_exceptions=True)

    async def _initialize_books(self) -> None:
        snapshots = await asyncio.gather(
            *(self._rest.get_depth_snapshot(symbol) for symbol in self._symbols)
        )
        async with self._book_lock:
            for snapshot in snapshots:
                book = BinanceDepthBook.from_snapshot(
                    snapshot,
                    depth=self._published_depth,
                )
                self._books[snapshot.symbol] = book
                for delta in self._depth_buffers[snapshot.symbol]:
                    event = book.apply(delta)
                    if event is not None:
                        self._emit(event)
                self._depth_buffers[snapshot.symbol] = []
            self._state = ConnectorState.READY
            self._ready.set()

    async def _read_messages(self, socket: _Socket) -> None:
        async for raw in socket:
            observed_at = self._observed_now()
            require_utc(observed_at, "observed_now")
            parsed = parse_websocket_message(raw, observed_at=observed_at)
            if parsed is None:
                continue
            if isinstance(parsed, BinanceDepthDelta):
                async with self._book_lock:
                    book = self._books.get(parsed.symbol)
                    if book is None:
                        buffer = self._depth_buffers[parsed.symbol]
                        if len(buffer) >= self._depth_buffer_size:
                            raise ConnectorHalt(
                                "depth_buffer_saturated",
                                f"depth buffer saturated for {parsed.symbol}",
                            )
                        buffer.append(parsed)
                        continue
                    event = book.apply(parsed)
                    if event is not None:
                        self._emit(event)
                continue
            self._emit(parsed)

    def _emit(self, event: ObservedMarketEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise ConnectorHalt(
                "output_queue_saturated",
                f"output queue saturated before event {event.event_id}",
            ) from exc

    def _set_halt(self, halt: ConnectorHalt) -> None:
        self._running = False
        detected_at = self._observed_now()
        require_utc(detected_at, "observed_now")
        self._failure = ConnectorFailure(
            reason_code=halt.reason_code,
            detail=halt.detail,
            error_type=type(halt).__name__,
            detected_at=detected_at,
            requires_operator_review=True,
        )
        self._state = ConnectorState.HALTED

    async def _halt_from_outside(self, halt: ConnectorHalt) -> None:
        self._set_halt(halt)
        if self._socket is not None:
            await self._socket.close()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
