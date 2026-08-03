import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from maais.market_data.events import MarketEventKind, MarkFundingPayload, PriceLevel
from maais.orchestration.observations import (
    EligibleBookTimeout,
    MarketObservationBuffer,
    RuntimeHealthRegistry,
    RuntimeObservationConflict,
)
from tests.unit.market_data.test_frame_builder import NOW, _book, _inputs


def _mark(offset_ms: int):
    event = next(item for item in _inputs() if item.kind is MarketEventKind.MARK_FUNDING)
    payload = event.payload
    assert isinstance(payload, MarkFundingPayload)
    observed_at = NOW + timedelta(milliseconds=offset_ms)
    return replace(
        event,
        event_id=f"mark-{offset_ms}",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        payload=replace(payload, mark_price=Decimal("100.5")),
    )


def test_health_registry_is_explicit_fail_closed_and_rejects_time_regression() -> None:
    registry = RuntimeHealthRegistry(("market_data", "execution"))
    registry.heartbeat("market_data", NOW)

    assert tuple(item.component for item in registry.snapshot()) == ("market_data",)
    registry.failure("execution", "book feed unavailable", NOW + timedelta(seconds=1))
    by_component = {item.component: item for item in registry.snapshot()}
    assert not by_component["execution"].healthy
    with pytest.raises(RuntimeObservationConflict, match="regressed"):
        registry.heartbeat("execution", NOW)
    with pytest.raises(ValueError, match="unknown"):
        registry.heartbeat("unknown", NOW)


async def test_future_book_wait_uses_first_genuinely_later_observation() -> None:
    buffer = MarketObservationBuffer(("BTCUSDT",))
    await buffer.observe(_mark(100))
    eligible_after = NOW + timedelta(milliseconds=200)
    waiter = asyncio.create_task(
        buffer.books_after(
            "BTCUSDT",
            eligible_after,
            timeout=timedelta(seconds=1),
        )
    )
    await asyncio.sleep(0)
    too_early = _book("book-early", 200, "100", "101", 200)
    future = _book("book-future", 201, "100", "101", 201)
    future = replace(
        future,
        payload=replace(
            future.payload,
            bids=(PriceLevel(Decimal("100"), Decimal("3")),),
            asks=(PriceLevel(Decimal("101"), Decimal("4")),),
        ),
    )
    assert await buffer.observe(too_early)
    assert await buffer.observe(future)

    books = await waiter

    assert tuple(item.event_id for item in books) == ("book-future",)
    assert books[0].mark_price == Decimal("100.5")
    assert books[0].bids[0].quantity == Decimal("3")


async def test_causal_book_snapshot_excludes_future_observations() -> None:
    buffer = MarketObservationBuffer(("BTCUSDT",))
    await buffer.observe(_mark(100))
    prior = _book("book-prior", 150, "100", "101", 150)
    cutoff = NOW + timedelta(milliseconds=200)
    future = _book("book-future", 201, "99", "102", 201)
    await buffer.observe(prior)
    await buffer.observe(future)

    books = buffer.books_at_or_before("BTCUSDT", cutoff)

    assert tuple(item.event_id for item in books) == ("book-prior",)


async def test_book_requires_prior_mark_and_wait_timeout_is_visible() -> None:
    buffer = MarketObservationBuffer(("BTCUSDT",))
    assert not await buffer.observe(_book("book-no-mark", 100, "100", "101", 100))
    with pytest.raises(EligibleBookTimeout, match="no eligible"):
        await buffer.books_after(
            "BTCUSDT",
            NOW,
            timeout=timedelta(milliseconds=1),
        )


async def test_primary_reference_is_causal_and_latest_observations_do_not_regress() -> None:
    buffer = MarketObservationBuffer(("BTCUSDT",))
    reference = next(
        item
        for item in _inputs()
        if item.kind is MarketEventKind.REFERENCE_PRICE and item.symbol == "BTCUSDT"
    )
    assert await buffer.observe(reference)
    assert (
        buffer.latest_primary_reference("BTCUSDT", at_or_before=reference.observed_at) == reference
    )
    assert (
        buffer.latest_primary_reference(
            "BTCUSDT", at_or_before=reference.observed_at - timedelta(microseconds=1)
        )
        is None
    )
    await buffer.observe(_mark(100))
    with pytest.raises(RuntimeObservationConflict, match="regressed"):
        await buffer.observe(
            replace(
                _mark(100),
                event_id="older-mark",
                venue_event_at=NOW - timedelta(seconds=1),
                observed_at=NOW - timedelta(milliseconds=999),
            )
        )
