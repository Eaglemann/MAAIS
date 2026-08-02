from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.market_data.events import (
    ClosedBarPayload,
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
    OrderBookPayload,
    PriceLevel,
    ReferenceKind,
    ReferencePricePayload,
    SymbolStatePayload,
    VenueClockPayload,
)
from maais.market_data.frames import CausalMinuteFrameBuilder, FrameIdentityConflict, FrameKey

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _event(
    kind: MarketEventKind,
    event_id: str,
    payload: object,
    *,
    observed_offset_ms: int,
    sequence: int | None,
    venue: str = "binance_futures",
    stream: str | None = None,
) -> ObservedMarketEvent:
    observed_at = NOW + timedelta(milliseconds=observed_offset_ms)
    return ObservedMarketEvent(
        venue=venue,
        stream=stream or kind.value,
        symbol="BTCUSDT",
        event_id=event_id,
        kind=kind,
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=sequence,
        sequence_not_applicable_reason=(
            "source_contract_has_no_sequence" if sequence is None else None
        ),
        payload=payload,
    )


def _bar() -> ObservedMarketEvent:
    return _event(
        MarketEventKind.CLOSED_BAR,
        "bar-1200",
        ClosedBarPayload(
            timeframe="1m",
            bar_open_at=NOW - timedelta(minutes=1),
            bar_close_at=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("12"),
            quote_volume=Decimal("1205"),
            trade_count=50,
            taker_buy_volume=Decimal("7"),
            taker_buy_quote_volume=Decimal("704"),
            closed=True,
        ),
        observed_offset_ms=100,
        sequence=100,
        stream="kline_1m",
    )


def _book(event_id: str, offset: int, bid: str, ask: str, sequence: int) -> ObservedMarketEvent:
    return _event(
        MarketEventKind.ORDER_BOOK,
        event_id,
        OrderBookPayload(
            bids=(PriceLevel(Decimal(bid), Decimal("2")),),
            asks=(PriceLevel(Decimal(ask), Decimal("2")),),
            published_at=NOW,
            sequence_start=sequence,
            previous_sequence=sequence - 1,
            snapshot_sequence=sequence - 10,
        ),
        observed_offset_ms=offset,
        sequence=sequence,
        stream="depth20",
    )


def _inputs() -> tuple[ObservedMarketEvent, ...]:
    return (
        _bar(),
        _book("book-before", 90, "100", "101", 90),
        _event(
            MarketEventKind.MARK_FUNDING,
            "mark-before",
            MarkFundingPayload(
                mark_price=Decimal("100.5"),
                index_price=Decimal("100.4"),
                funding_rate=Decimal("0.0001"),
                next_funding_at=NOW + timedelta(hours=4),
                estimated_settle_price=None,
            ),
            observed_offset_ms=95,
            sequence=None,
            stream="mark_price",
        ),
        _event(
            MarketEventKind.REFERENCE_PRICE,
            "spot-before",
            ReferencePricePayload(
                reference_kind=ReferenceKind.PRIMARY_SPOT,
                instrument="BTCUSDT",
                price=Decimal("100.45"),
                source_event_id="spot-trade-1",
                source_quantity=Decimal("1"),
                source_side=None,
                source_bid=None,
                source_ask=None,
                source_published_at=None,
            ),
            observed_offset_ms=80,
            sequence=80,
            venue="binance_spot",
            stream="spot_ticker",
        ),
        _event(
            MarketEventKind.REFERENCE_PRICE,
            "secondary-before",
            ReferencePricePayload(
                reference_kind=ReferenceKind.SECONDARY_VENUE,
                instrument="BTC-USD",
                price=Decimal("100.4"),
                source_event_id="secondary-trade-1",
                source_quantity=Decimal("1"),
                source_side=None,
                source_bid=None,
                source_ask=None,
                source_published_at=None,
            ),
            observed_offset_ms=85,
            sequence=85,
            venue="coinbase",
            stream="ticker",
        ),
        _event(
            MarketEventKind.VENUE_CLOCK,
            "clock-before",
            VenueClockPayload(server_time=NOW + timedelta(milliseconds=69)),
            observed_offset_ms=70,
            sequence=69,
            stream="server_time",
        ),
        _event(
            MarketEventKind.SYMBOL_STATE,
            "symbol-before",
            SymbolStatePayload(status="TRADING"),
            observed_offset_ms=70,
            sequence=70,
            stream="exchange_info",
        ),
    )


def _key() -> FrameKey:
    return FrameKey(
        experiment_id=UUID(int=1),
        strategy_version_id=UUID(int=2),
        symbol="BTCUSDT",
        timeframe="1m",
        bar_close_at=NOW,
    )


def test_shuffled_inputs_produce_identical_frame_and_source_manifest() -> None:
    inputs = _inputs()
    left = CausalMinuteFrameBuilder().build(_key(), inputs[0], inputs)
    right = CausalMinuteFrameBuilder().build(_key(), inputs[0], tuple(reversed(inputs)))

    assert left == right
    assert left.content_hash == right.content_hash
    assert left.best_bid == Decimal("100")
    assert left.mark_price == Decimal("100.5")
    assert set(left.source_manifest) == {
        "closed_bar",
        "order_book",
        "mark_funding",
        "primary_spot",
        "secondary_venue",
        "venue_clock",
        "symbol_state",
    }


def test_future_book_and_reference_cannot_change_prior_frame() -> None:
    inputs = _inputs()
    baseline = CausalMinuteFrameBuilder().build(_key(), inputs[0], inputs)
    future = (
        _book("book-future", 101, "50", "51", 101),
        _event(
            MarketEventKind.REFERENCE_PRICE,
            "secondary-future",
            ReferencePricePayload(
                reference_kind=ReferenceKind.SECONDARY_VENUE,
                instrument="BTC-USD",
                price=Decimal("500"),
                source_event_id="secondary-trade-future",
                source_quantity=Decimal("1"),
                source_side=None,
                source_bid=None,
                source_ask=None,
                source_published_at=None,
            ),
            observed_offset_ms=102,
            sequence=102,
            venue="coinbase",
            stream="ticker",
        ),
    )
    mutated = CausalMinuteFrameBuilder().build(_key(), inputs[0], (*inputs, *future))

    assert mutated == baseline
    assert mutated.content_hash == baseline.content_hash


def test_identical_duplicate_is_idempotent_but_conflicting_duplicate_fails() -> None:
    inputs = _inputs()
    duplicate = inputs[1]
    result = CausalMinuteFrameBuilder().build(_key(), inputs[0], (*inputs, duplicate))

    assert result.best_bid == Decimal("100")
    with pytest.raises(FrameIdentityConflict, match="different content"):
        CausalMinuteFrameBuilder().build(
            _key(),
            inputs[0],
            (
                *inputs,
                replace(
                    duplicate,
                    payload=replace(
                        duplicate.payload, bids=(PriceLevel(Decimal("99"), Decimal("2")),)
                    ),
                ),
            ),  # type: ignore[arg-type]
        )


def test_frame_rejects_open_wrong_interval_and_key_mismatch() -> None:
    inputs = _inputs()
    bar = inputs[0]
    assert isinstance(bar.payload, ClosedBarPayload)
    with pytest.raises(ValueError, match="closed"):
        CausalMinuteFrameBuilder().build(
            _key(),
            replace(bar, payload=replace(bar.payload, closed=False)),
            inputs,
        )
    with pytest.raises(ValueError, match="one-minute"):
        CausalMinuteFrameBuilder().build(
            _key(),
            replace(
                bar,
                payload=replace(
                    bar.payload,
                    timeframe="5m",
                    bar_open_at=NOW - timedelta(minutes=5),
                ),
            ),
            inputs,
        )
    with pytest.raises(ValueError, match="key"):
        CausalMinuteFrameBuilder().build(
            replace(_key(), symbol="ETHUSDT"),
            bar,
            inputs,
        )
