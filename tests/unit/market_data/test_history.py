from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from maais.market_data.events import ClosedBarPayload, MarketEventKind, PriceLevel
from maais.market_data.frames import CausalMinuteFrameBuilder
from maais.market_data.history import (
    CausalFrameHistory,
    CommittedFrameSnapshot,
    FrameHistoryConflict,
)
from tests.unit.market_data.test_frame_builder import NOW, _inputs, _key


def _history_snapshots(count: int = 60) -> tuple[CommittedFrameSnapshot, ...]:
    snapshots = []
    for index in range(count):
        close_at = NOW - timedelta(minutes=count - index)
        price = Decimal("95") + Decimal(index) / Decimal("100")
        snapshots.append(
            CommittedFrameSnapshot(
                experiment_id=UUID(int=1),
                frame_id=UUID(int=index + 10),
                symbol="BTCUSDT",
                timeframe="1m",
                bar=ClosedBarPayload(
                    timeframe="1m",
                    bar_open_at=close_at - timedelta(minutes=1),
                    bar_close_at=close_at,
                    open=price,
                    high=price + Decimal("1"),
                    low=price - Decimal("1"),
                    close=price + Decimal("0.1"),
                    volume=Decimal("10"),
                    quote_volume=Decimal("1000"),
                    trade_count=10,
                    taker_buy_volume=Decimal("5"),
                    taker_buy_quote_volume=Decimal("500"),
                    closed=True,
                ),
                source_sequences={"closed_bar": index + 39, "order_book": index + 30},
                content_hash=f"{index + 1:064x}",
            )
        )
    return tuple(snapshots)


def _frame_with_uneven_depth():
    events = list(_inputs())
    book_index = next(
        index for index, event in enumerate(events) if event.kind is MarketEventKind.ORDER_BOOK
    )
    book = events[book_index]
    events[book_index] = replace(
        book,
        payload=replace(
            book.payload,
            bids=(PriceLevel(Decimal("100"), Decimal("3")),),
            asks=(PriceLevel(Decimal("101"), Decimal("1")),),
        ),
    )
    return CausalMinuteFrameBuilder().build(_key(), events[0], tuple(events))


def test_feature_staging_is_causal_and_does_not_mutate_committed_history() -> None:
    history = CausalFrameHistory(UUID(int=1), ("BTCUSDT",))
    history.restore(_history_snapshots())
    frame = _frame_with_uneven_depth()
    before = history.snapshots("BTCUSDT")

    features = history.compute(frame)
    integrity = history.integrity_context(
        frame,
        evaluated_at=frame.cutoff_at + timedelta(milliseconds=100),
    )

    assert history.snapshots("BTCUSDT") == before
    assert features is not None
    assert features.timestamp == frame.bar.bar_close_at
    assert features.book_imbalance == pytest.approx(0.5)
    assert integrity.historical_bar_count == 60
    assert integrity.previous_bar_close_at == frame.bar.bar_open_at
    assert integrity.prior_sequences == {"closed_bar": 98, "order_book": 89}
    assert len(integrity.recent_close_returns) == 59


def test_committed_history_restores_identical_features_and_is_idempotent() -> None:
    frame = _frame_with_uneven_depth()
    history = CausalFrameHistory(UUID(int=1), ("BTCUSDT",))
    history.restore(_history_snapshots())
    expected = history.compute(frame)

    assert history.commit(frame)
    assert not history.commit(frame)
    restarted = CausalFrameHistory(UUID(int=1), ("BTCUSDT",))
    restarted.restore(history.snapshots("BTCUSDT"))

    assert restarted.compute(frame) == expected


def test_restore_and_commit_reject_noncontiguous_or_conflicting_history() -> None:
    values = list(_history_snapshots(60))
    values[-1] = replace(
        values[-1],
        bar=replace(
            values[-1].bar,
            bar_open_at=values[-1].bar.bar_open_at + timedelta(seconds=30),
        ),
    )
    with pytest.raises(FrameHistoryConflict, match="not contiguous"):
        CausalFrameHistory(UUID(int=1), ("BTCUSDT",)).restore(values)

    frame = _frame_with_uneven_depth()
    history = CausalFrameHistory(UUID(int=1), ("BTCUSDT",))
    history.restore(_history_snapshots())
    assert history.commit(frame)
    with pytest.raises(FrameHistoryConflict, match="different committed content"):
        history.commit(replace(frame, content_hash="f" * 64))
