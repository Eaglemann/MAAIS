from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.domain.json import canonical_json_bytes
from maais.market_data.events import (
    ClosedBarPayload,
    MarketEventKind,
    ObservedMarketEvent,
    OrderBookPayload,
    PriceLevel,
    ReferenceKind,
    ReferencePricePayload,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _identity(kind: MarketEventKind, payload: object) -> ObservedMarketEvent:
    return ObservedMarketEvent(
        venue="binance_futures",
        stream="btcusdt@kline_1m",
        symbol="BTCUSDT",
        event_id="event-1",
        kind=kind,
        venue_event_at=NOW,
        observed_at=NOW + timedelta(milliseconds=5),
        sequence=10,
        sequence_not_applicable_reason=None,
        payload=payload,
    )


def _bar() -> ClosedBarPayload:
    return ClosedBarPayload(
        timeframe="1m",
        bar_open_at=NOW - timedelta(minutes=1),
        bar_close_at=NOW,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("12.5"),
        quote_volume=Decimal("1260"),
        trade_count=42,
        taker_buy_volume=Decimal("7"),
        taker_buy_quote_volume=Decimal("706"),
        closed=True,
    )


def test_event_is_canonical_and_financial_values_are_decimal_only() -> None:
    event = _identity(MarketEventKind.CLOSED_BAR, _bar())

    assert event.content_hash == event.content_hash
    assert canonical_json_bytes(event.to_dict()) == canonical_json_bytes(event.to_dict())
    with pytest.raises(ValueError, match="Decimal"):
        replace(_bar(), close=101.0)  # type: ignore[arg-type]


def test_sequence_is_required_or_explicitly_not_applicable() -> None:
    with pytest.raises(ValueError, match="sequence"):
        replace(
            _identity(MarketEventKind.CLOSED_BAR, _bar()),
            sequence=None,
            sequence_not_applicable_reason=None,
        )
    reference = ObservedMarketEvent(
        venue="coinbase",
        stream="ticker",
        symbol="BTCUSDT",
        event_id="coinbase-btc-1",
        kind=MarketEventKind.REFERENCE_PRICE,
        venue_event_at=NOW,
        observed_at=NOW + timedelta(milliseconds=5),
        sequence=None,
        sequence_not_applicable_reason="source_has_no_sequence",
        payload=ReferencePricePayload(
            reference_kind=ReferenceKind.SECONDARY_VENUE,
            instrument="BTC-USD",
            price=Decimal("100.5"),
            source_event_id="coinbase-trade-1",
            source_quantity=Decimal("1"),
            source_side=None,
            source_bid=None,
            source_ask=None,
            source_published_at=None,
        ),
    )

    assert reference.sequence is None


def test_event_rejects_kind_payload_mismatch_and_naive_time() -> None:
    book = OrderBookPayload(
        bids=(PriceLevel(Decimal("100"), Decimal("1")),),
        asks=(PriceLevel(Decimal("101"), Decimal("1")),),
        published_at=NOW,
        sequence_start=10,
        previous_sequence=9,
        snapshot_sequence=1,
    )
    with pytest.raises(ValueError, match="payload"):
        _identity(MarketEventKind.CLOSED_BAR, book)
    with pytest.raises(ValueError, match="UTC"):
        replace(
            _identity(MarketEventKind.ORDER_BOOK, book),
            observed_at=datetime(2026, 8, 2, 12),
        )


def test_order_book_rejects_crossed_unsorted_and_float_levels() -> None:
    with pytest.raises(ValueError, match="crossed"):
        OrderBookPayload(
            bids=(PriceLevel(Decimal("101"), Decimal("1")),),
            asks=(PriceLevel(Decimal("100"), Decimal("1")),),
            published_at=NOW,
            sequence_start=10,
            previous_sequence=9,
            snapshot_sequence=1,
        )
    with pytest.raises(ValueError, match="descending"):
        OrderBookPayload(
            bids=(
                PriceLevel(Decimal("99"), Decimal("1")),
                PriceLevel(Decimal("100"), Decimal("1")),
            ),
            asks=(PriceLevel(Decimal("101"), Decimal("1")),),
            published_at=NOW,
            sequence_start=10,
            previous_sequence=9,
            snapshot_sequence=1,
        )
    with pytest.raises(ValueError, match="Decimal"):
        PriceLevel(100.0, Decimal("1"))  # type: ignore[arg-type]
