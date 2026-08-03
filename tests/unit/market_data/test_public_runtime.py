import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.market_data.events import (
    FundingSettlementPayload,
    MarketEventKind,
    ObservedMarketEvent,
    ReferenceKind,
    ReferencePricePayload,
    SymbolStatePayload,
    VenueClockPayload,
)
from maais.market_data.public_runtime import (
    PublicDataHalt,
    PublicDataRuntimeState,
    PublicMarketDataRuntime,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
_END = object()


def _event(kind: MarketEventKind, event_id: str) -> ObservedMarketEvent:
    if kind is MarketEventKind.VENUE_CLOCK:
        payload = VenueClockPayload(server_time=NOW)
        venue = "binance_usdm"
    elif kind is MarketEventKind.SYMBOL_STATE:
        payload = SymbolStatePayload(status="TRADING")
        venue = "binance_usdm"
    else:
        reference_kind = (
            ReferenceKind.PRIMARY_SPOT
            if event_id.startswith("primary")
            else ReferenceKind.SECONDARY_VENUE
        )
        payload = ReferencePricePayload(
            reference_kind=reference_kind,
            instrument="BTCUSDT",
            price=Decimal("100.5"),
            source_event_id=event_id,
            source_quantity=None,
            source_side=None,
            source_bid=Decimal("100"),
            source_ask=Decimal("101"),
            source_published_at=NOW,
            source_engine_at=None,
        )
        venue = "binance_spot" if reference_kind is ReferenceKind.PRIMARY_SPOT else "bybit_spot"
    return ObservedMarketEvent(
        venue=venue,
        stream="fixture",
        symbol="BTCUSDT",
        event_id=event_id,
        kind=kind,
        venue_event_at=NOW,
        observed_at=NOW + timedelta(milliseconds=1),
        sequence=None,
        sequence_not_applicable_reason="fixture_has_no_sequence",
        payload=payload,
    )


def _funding_event(event_id: str) -> ObservedMarketEvent:
    funding_at = NOW - timedelta(hours=1)
    return ObservedMarketEvent(
        venue="binance_usdm",
        stream="rest:/fapi/v1/fundingRate",
        symbol="BTCUSDT",
        event_id=event_id,
        kind=MarketEventKind.FUNDING_SETTLEMENT,
        venue_event_at=funding_at,
        observed_at=NOW + timedelta(seconds=1),
        sequence=None,
        sequence_not_applicable_reason="binance_funding_history_has_no_sequence",
        payload=FundingSettlementPayload(
            funding_at=funding_at,
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("100.5"),
            rate_type="Regular",
        ),
    )


@dataclass(frozen=True)
class _FuturesPreflight:
    venue_clocks: tuple[ObservedMarketEvent, ...]
    symbol_states: tuple[ObservedMarketEvent, ...]


class _FuturesRest:
    preflight_complete = True

    def __init__(self) -> None:
        self.calls = 0
        self.funding_calls: list[tuple[str, int, int]] = []
        self.funding_events: tuple[ObservedMarketEvent, ...] = ()

    async def preflight(self, symbols: tuple[str, ...]) -> _FuturesPreflight:
        assert symbols == ("BTCUSDT",)
        self.calls += 1
        return _FuturesPreflight(
            (_event(MarketEventKind.VENUE_CLOCK, f"clock-{self.calls}"),),
            (_event(MarketEventKind.SYMBOL_STATE, f"state-{self.calls}"),),
        )

    async def get_funding_events(
        self,
        symbol: str,
        *,
        start_ms: int,
        end_ms: int,
    ) -> tuple[ObservedMarketEvent, ...]:
        self.funding_calls.append((symbol, start_ms, end_ms))
        return self.funding_events


class _Reference:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.calls = 0
        self.preflight_calls = 0

    async def preflight(self, symbols: tuple[str, ...]) -> tuple[str, ...]:
        assert symbols == ("BTCUSDT",)
        self.preflight_calls += 1
        return symbols

    async def get_reference_events(self) -> tuple[ObservedMarketEvent, ...]:
        self.calls += 1
        return (
            _event(
                MarketEventKind.REFERENCE_PRICE,
                f"{self.prefix}-{self.calls}",
            ),
        )


class _FailingReference(_Reference):
    async def get_reference_events(self) -> tuple[ObservedMarketEvent, ...]:
        if self.calls == 1:
            raise RuntimeError("reference transport retries exhausted")
        return await super().get_reference_events()


class _WebSocket:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.queue.put_nowait(_END)

    async def events(self) -> AsyncGenerator[ObservedMarketEvent, None]:
        while True:
            value = await self.queue.get()
            if value is _END:
                return
            assert isinstance(value, ObservedMarketEvent)
            yield value


async def _blocking_sleep(_: float) -> None:
    await asyncio.Event().wait()


async def test_runtime_preflights_every_source_and_pumps_one_managed_queue() -> None:
    rest = _FuturesRest()
    primary = _Reference("primary")
    secondary = _Reference("secondary")
    websocket = _WebSocket()
    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=rest,  # type: ignore[arg-type]
        primary_spot=primary,  # type: ignore[arg-type]
        secondary_spot=secondary,  # type: ignore[arg-type]
        websocket_factory=lambda symbols, supplied_rest: websocket,  # type: ignore[arg-type,return-value]
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_blocking_sleep,
        funding_start_at=NOW - timedelta(hours=8),
    )

    await runtime.start()
    websocket.queue.put_nowait(_event(MarketEventKind.VENUE_CLOCK, "websocket-event"))
    events = runtime.events()
    observed = [await anext(events) for _ in range(5)]

    assert runtime.state is PublicDataRuntimeState.READY
    assert websocket.started
    assert [event.event_id for event in observed[:4]] == [
        "clock-1",
        "state-1",
        "primary-1",
        "secondary-1",
    ]
    assert observed[4].event_id == "websocket-event"
    await runtime.stop()
    assert websocket.stopped
    assert runtime.state is PublicDataRuntimeState.STOPPED


async def test_runtime_refreshes_references_after_websocket_readiness() -> None:
    timeline: list[str] = []

    class TimelineReference(_Reference):
        async def get_reference_events(self) -> tuple[ObservedMarketEvent, ...]:
            timeline.append(self.prefix)
            return await super().get_reference_events()

    class TimelineWebSocket(_WebSocket):
        async def start(self) -> None:
            timeline.append("websocket")
            await super().start()

    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=_FuturesRest(),  # type: ignore[arg-type]
        primary_spot=TimelineReference("primary"),  # type: ignore[arg-type]
        secondary_spot=TimelineReference("secondary"),  # type: ignore[arg-type]
        websocket_factory=lambda symbols, rest: TimelineWebSocket(),  # type: ignore[arg-type,return-value]
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_blocking_sleep,
        funding_start_at=NOW - timedelta(hours=8),
    )

    await runtime.start()

    assert timeline[0] == "websocket"
    assert set(timeline[1:]) == {"primary", "secondary"}
    await runtime.stop()


async def test_runtime_queue_saturation_fails_startup_without_dropping() -> None:
    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=_FuturesRest(),  # type: ignore[arg-type]
        primary_spot=_Reference("primary"),  # type: ignore[arg-type]
        secondary_spot=_Reference("secondary"),  # type: ignore[arg-type]
        websocket_factory=lambda symbols, rest: _WebSocket(),  # type: ignore[arg-type,return-value]
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_blocking_sleep,
        funding_start_at=NOW - timedelta(hours=8),
        queue_size=1,
    )

    with pytest.raises(PublicDataHalt, match="startup"):
        await runtime.start()

    assert runtime.state is PublicDataRuntimeState.HALTED
    assert runtime.failure is not None
    assert runtime.failure.reason_code == "public_runtime_queue_saturated"


async def test_periodic_preflight_revalidates_every_public_source() -> None:
    rest = _FuturesRest()
    primary = _Reference("primary")
    secondary = _Reference("secondary")
    websocket = _WebSocket()
    refresh_released = False
    blocked = asyncio.Event()

    async def release_one_refresh(delay: float) -> None:
        nonlocal refresh_released
        if delay == 30 and not refresh_released:
            refresh_released = True
            return
        await blocked.wait()

    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=rest,  # type: ignore[arg-type]
        primary_spot=primary,  # type: ignore[arg-type]
        secondary_spot=secondary,  # type: ignore[arg-type]
        websocket_factory=lambda symbols, supplied_rest: websocket,  # type: ignore[arg-type,return-value]
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=release_one_refresh,
        funding_start_at=NOW - timedelta(hours=8),
        reference_poll_seconds=999,
        preflight_refresh_seconds=30,
    )

    await runtime.start()
    for _ in range(50):
        if rest.calls == 2:
            break
        await asyncio.sleep(0)

    assert rest.calls == 2
    assert primary.preflight_calls == 2
    assert secondary.preflight_calls == 2
    await runtime.stop()


async def test_runtime_polls_observed_funding_from_explicit_restart_cutoff() -> None:
    rest = _FuturesRest()
    funding = _funding_event("funding-observed-1")
    rest.funding_events = (funding,)
    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=rest,  # type: ignore[arg-type]
        primary_spot=_Reference("primary"),  # type: ignore[arg-type]
        secondary_spot=_Reference("secondary"),  # type: ignore[arg-type]
        websocket_factory=lambda symbols, supplied_rest: _WebSocket(),  # type: ignore[arg-type,return-value]
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=_blocking_sleep,
        funding_start_at=NOW - timedelta(hours=8),
        funding_poll_seconds=60,
    )

    await runtime.start()
    events = runtime.events()
    observed = [await anext(events) for _ in range(5)]

    assert funding.event_id in {event.event_id for event in observed}
    assert rest.funding_calls == [
        (
            "BTCUSDT",
            int((NOW - timedelta(hours=8)).timestamp() * 1000),
            int((NOW + timedelta(seconds=1)).timestamp() * 1000),
        )
    ]
    await runtime.stop()


async def test_runtime_advances_funding_window_after_each_successful_poll() -> None:
    rest = _FuturesRest()
    current_now = NOW + timedelta(seconds=1)
    released = False
    blocked = asyncio.Event()

    def observed_now() -> datetime:
        return current_now

    async def release_one_funding_poll(delay: float) -> None:
        nonlocal current_now, released
        if delay == 60 and not released:
            released = True
            current_now += timedelta(seconds=60)
            return
        await blocked.wait()

    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=rest,  # type: ignore[arg-type]
        primary_spot=_Reference("primary"),  # type: ignore[arg-type]
        secondary_spot=_Reference("secondary"),  # type: ignore[arg-type]
        websocket_factory=lambda symbols, supplied_rest: _WebSocket(),  # type: ignore[arg-type,return-value]
        observed_now=observed_now,
        sleep=release_one_funding_poll,
        funding_start_at=NOW - timedelta(hours=8),
        funding_poll_seconds=60,
    )

    await runtime.start()
    for _ in range(50):
        if len(rest.funding_calls) == 2:
            break
        await asyncio.sleep(0)

    first_end = int((NOW + timedelta(seconds=1)).timestamp() * 1000)
    assert rest.funding_calls == [
        (
            "BTCUSDT",
            int((NOW - timedelta(hours=8)).timestamp() * 1000),
            first_end,
        ),
        (
            "BTCUSDT",
            first_end + 1,
            int((NOW + timedelta(seconds=61)).timestamp() * 1000),
        ),
    ]
    await runtime.stop()


async def test_runtime_halt_preserves_the_failed_task_identity() -> None:
    release_reference = True
    blocked = asyncio.Event()

    async def release_one_reference_poll(delay: float) -> None:
        nonlocal release_reference
        if delay == 1 and release_reference:
            release_reference = False
            return
        await blocked.wait()

    runtime = PublicMarketDataRuntime(
        ("BTCUSDT",),
        futures_rest=_FuturesRest(),  # type: ignore[arg-type]
        primary_spot=_FailingReference("primary"),  # type: ignore[arg-type]
        secondary_spot=_Reference("secondary"),  # type: ignore[arg-type]
        websocket_factory=lambda symbols, supplied_rest: _WebSocket(),  # type: ignore[arg-type,return-value]
        observed_now=lambda: NOW + timedelta(seconds=1),
        sleep=release_one_reference_poll,
        funding_start_at=NOW - timedelta(hours=8),
        reference_poll_seconds=1,
        funding_poll_seconds=60,
        preflight_refresh_seconds=30,
    )

    await runtime.start()
    await runtime.wait_closed()

    assert runtime.state is PublicDataRuntimeState.HALTED
    assert runtime.failure is not None
    assert runtime.failure.reason_code == "public_data_task_failed"
    assert runtime.failure.error_type == "PublicDataHalt"
    assert runtime.failure.detail == (
        "task=public_reference_poll; RuntimeError: reference transport retries exhausted"
    )
