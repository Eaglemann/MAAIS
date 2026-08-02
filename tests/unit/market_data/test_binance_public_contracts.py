import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from maais.market_data.connectors.binance_contracts import (
    BinanceContractError,
    BinanceDepthBook,
    BinanceDepthDelta,
    BinanceDepthSnapshot,
    BinanceSequenceGap,
    parse_websocket_message,
)
from maais.market_data.events import (
    AggressorSide,
    ClosedBarPayload,
    MarketEventKind,
    MarkFundingPayload,
    OrderBookPayload,
    TradePayload,
)

OBSERVED_AT = datetime(2026, 8, 2, 12, 1, 0, 50_000, tzinfo=timezone.utc)


def _combined(stream: str, data: dict[str, object]) -> str:
    return json.dumps({"stream": stream, "data": data})


def _depth(
    *,
    first: int = 101,
    final: int = 102,
    previous: int = 100,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    observed_at: datetime = OBSERVED_AT,
) -> BinanceDepthDelta:
    parsed = parse_websocket_message(
        _combined(
            "btcusdt@depth@500ms",
            {
                "e": "depthUpdate",
                "E": 1785672050000,
                "T": 1785672049998,
                "s": "BTCUSDT",
                "U": first,
                "u": final,
                "pu": previous,
                "b": bids or [["100", "2"], ["99", "3"]],
                "a": asks or [["101", "2"], ["102", "3"]],
            },
        ),
        observed_at=observed_at,
    )
    assert isinstance(parsed, BinanceDepthDelta)
    return parsed


def test_closed_kline_contract_emits_only_final_bar_with_replay_sequence() -> None:
    data = {
        "e": "kline",
        "E": 1785672060010,
        "s": "BTCUSDT",
        "k": {
            "t": 1785672000000,
            "T": 1785672059999,
            "s": "BTCUSDT",
            "i": "1m",
            "f": 100,
            "L": 125,
            "o": "100",
            "c": "101",
            "h": "102",
            "l": "99",
            "v": "12.5",
            "n": 26,
            "x": True,
            "q": "1260",
            "V": "7",
            "Q": "706",
            "B": "0",
        },
    }

    event = parse_websocket_message(
        _combined("btcusdt@kline_1m", data),
        observed_at=OBSERVED_AT + timedelta(seconds=1),
    )

    assert event is not None and not isinstance(event, BinanceDepthDelta)
    assert event.kind is MarketEventKind.CLOSED_BAR
    assert event.sequence == 1785672000000 // 60_000
    assert event.venue_event_at == datetime.fromtimestamp(
        1785672060010 / 1000,
        tz=timezone.utc,
    )
    assert isinstance(event.payload, ClosedBarPayload)
    assert event.payload.bar_close_at == datetime.fromtimestamp(
        1785672060000 / 1000,
        tz=timezone.utc,
    )
    assert event.payload.closed

    assert isinstance(data["k"], dict)
    open_update = {**data["k"], "x": False}
    assert (
        parse_websocket_message(
            _combined("btcusdt@kline_1m", {**data, "k": open_update}),
            observed_at=OBSERVED_AT,
        )
        is None
    )


def test_mark_contract_treats_T_as_next_funding_time_and_has_no_sequence() -> None:
    event = parse_websocket_message(
        _combined(
            "btcusdt@markPrice@1s",
            {
                "e": "markPriceUpdate",
                "E": 1785672050000,
                "s": "BTCUSDT",
                "p": "100.25",
                "i": "100.10",
                "P": "100.20",
                "r": "0.0001",
                "T": 1785700800000,
            },
        ),
        observed_at=OBSERVED_AT,
    )

    assert event is not None and not isinstance(event, BinanceDepthDelta)
    assert event.kind is MarketEventKind.MARK_FUNDING
    assert event.sequence is None
    assert event.sequence_not_applicable_reason == "binance_mark_stream_has_no_sequence"
    assert isinstance(event.payload, MarkFundingPayload)
    assert event.payload.next_funding_at == datetime.fromtimestamp(
        1785700800000 / 1000,
        tz=timezone.utc,
    )
    assert event.payload.estimated_settle_price == Decimal("100.20")


def test_aggregate_trade_uses_official_aggregate_id_and_aggressor_side() -> None:
    event = parse_websocket_message(
        _combined(
            "btcusdt@aggTrade",
            {
                "e": "aggTrade",
                "E": 1785672050002,
                "a": 321,
                "s": "BTCUSDT",
                "p": "100.5",
                "q": "0.4",
                "f": 800,
                "l": 802,
                "T": 1785672050001,
                "m": True,
            },
        ),
        observed_at=OBSERVED_AT,
    )

    assert event is not None and not isinstance(event, BinanceDepthDelta)
    assert event.kind is MarketEventKind.TRADE
    assert event.sequence == 321
    assert isinstance(event.payload, TradePayload)
    assert event.payload.aggressor_side is AggressorSide.SELL


def test_depth_book_reconciles_snapshot_ranges_and_retains_venue_metadata() -> None:
    snapshot = BinanceDepthSnapshot(
        symbol="BTCUSDT",
        last_update_id=100,
        published_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        venue_event_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        observed_at=OBSERVED_AT - timedelta(seconds=1),
        bids=((Decimal("100"), Decimal("1")), (Decimal("99"), Decimal("3"))),
        asks=((Decimal("101"), Decimal("1")), (Decimal("102"), Decimal("3"))),
    )
    book = BinanceDepthBook.from_snapshot(snapshot, depth=2)

    event = book.apply(_depth())

    assert event is not None
    assert event.kind is MarketEventKind.ORDER_BOOK
    assert event.sequence == 102
    assert isinstance(event.payload, OrderBookPayload)
    assert event.payload.bids[0].quantity == Decimal("2")
    assert event.payload.sequence_start == 101
    assert event.payload.previous_sequence == 100
    assert event.payload.snapshot_sequence == 100
    assert event.payload.published_at == datetime.fromtimestamp(
        1785672050000 / 1000,
        tz=timezone.utc,
    )

    second = book.apply(
        _depth(
            first=103,
            final=104,
            previous=102,
            bids=[["100", "0"], ["98", "4"]],
            asks=[["101", "2"]],
            observed_at=OBSERVED_AT + timedelta(milliseconds=500),
        )
    )
    assert second is not None and isinstance(second.payload, OrderBookPayload)
    assert second.payload.bids[0].price == Decimal("99")


def test_depth_book_rejects_gap_in_previous_update_chain() -> None:
    snapshot = BinanceDepthSnapshot(
        symbol="BTCUSDT",
        last_update_id=100,
        published_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        venue_event_at=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        observed_at=OBSERVED_AT - timedelta(seconds=1),
        bids=((Decimal("100"), Decimal("1")),),
        asks=((Decimal("101"), Decimal("1")),),
    )
    book = BinanceDepthBook.from_snapshot(snapshot, depth=1)
    assert book.apply(_depth()) is not None

    with pytest.raises(BinanceSequenceGap, match="previous final update"):
        book.apply(
            _depth(
                first=103,
                final=104,
                previous=103,
                observed_at=OBSERVED_AT + timedelta(milliseconds=500),
            )
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"E": None},
        {"T": None},
        {"U": None},
        {"u": None},
        {"pu": None},
        {"b": None},
        {"a": None},
    ),
)
def test_depth_contract_has_no_timestamp_sequence_or_book_defaults(
    mutation: dict[str, None],
) -> None:
    data: dict[str, object] = {
        "e": "depthUpdate",
        "E": 1785672050000,
        "T": 1785672049998,
        "s": "BTCUSDT",
        "U": 101,
        "u": 102,
        "pu": 100,
        "b": [["100", "2"]],
        "a": [["101", "2"]],
    }
    data.update(mutation)

    with pytest.raises(BinanceContractError):
        parse_websocket_message(
            _combined("btcusdt@depth@500ms", data),
            observed_at=OBSERVED_AT,
        )


def test_partial_depth_shape_and_stream_symbol_mismatch_fail_closed() -> None:
    with pytest.raises(BinanceContractError, match="unsupported stream"):
        parse_websocket_message(
            _combined(
                "btcusdt@depth20@500ms",
                {"lastUpdateId": 100, "bids": [["100", "1"]], "asks": [["101", "1"]]},
            ),
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(BinanceContractError, match="symbol"):
        parse_websocket_message(
            _combined(
                "ethusdt@aggTrade",
                {
                    "e": "aggTrade",
                    "E": 1785672050002,
                    "a": 321,
                    "s": "BTCUSDT",
                    "p": "100.5",
                    "q": "0.4",
                    "f": 800,
                    "l": 802,
                    "T": 1785672050001,
                    "m": True,
                },
            ),
            observed_at=OBSERVED_AT,
        )
