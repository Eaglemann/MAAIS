"""Restart-safe causal candle history for official feature and integrity evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from maais.feature_pipeline.features import FeatureSet
from maais.feature_pipeline.pipeline import FeaturePipeline
from maais.market_data.events import ClosedBarPayload
from maais.market_data.frames import CausalMinuteFrame
from maais.market_data.integrity.state_machine import IntegrityContext
from maais.market_data.schemas import FundingRateData, KlineData, OrderBookSnapshot


class FrameHistoryConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CommittedFrameSnapshot:
    """The persisted subset needed to reproduce rolling causal calculations."""

    experiment_id: UUID
    frame_id: UUID
    symbol: str
    timeframe: str
    bar: ClosedBarPayload
    primary_spot_price: Decimal | None
    primary_spot_event_id: str | None
    primary_spot_observed_at: datetime | None
    source_sequences: Mapping[str, int]
    content_hash: str

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or self.frame_id.int == 0:
            raise ValueError("history snapshot UUIDs cannot be nil")
        if not self.symbol or self.symbol != self.symbol.upper() or not self.timeframe:
            raise ValueError("history snapshot identity is invalid")
        if self.bar.timeframe != self.timeframe or not self.bar.closed:
            raise ValueError("history snapshot requires a matching closed bar")
        spot_values = (
            self.primary_spot_price,
            self.primary_spot_event_id,
            self.primary_spot_observed_at,
        )
        if any(value is None for value in spot_values) != all(
            value is None for value in spot_values
        ):
            raise ValueError("history primary spot evidence must be complete")
        if self.primary_spot_price is not None and (
            not self.primary_spot_price.is_finite() or self.primary_spot_price <= 0
        ):
            raise ValueError("history primary spot price must be positive and finite")
        if self.primary_spot_observed_at is not None and (
            self.primary_spot_observed_at.tzinfo is None
            or self.primary_spot_observed_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("history primary spot observation must be UTC-aware")
        if any(not name or value < 0 for name, value in self.source_sequences.items()):
            raise ValueError("history source sequences must be named and nonnegative")
        if len(self.content_hash) != 64:
            raise ValueError("history content_hash must be SHA-256")
        object.__setattr__(
            self,
            "source_sequences",
            MappingProxyType(dict(self.source_sequences)),
        )

    @classmethod
    def from_frame(cls, frame: CausalMinuteFrame) -> CommittedFrameSnapshot:
        spot_source = frame.source_manifest.get("primary_spot")
        return cls(
            experiment_id=frame.key.experiment_id,
            frame_id=frame.frame_id,
            symbol=frame.key.symbol,
            timeframe=frame.key.timeframe,
            bar=frame.bar,
            primary_spot_price=frame.primary_spot_price,
            primary_spot_event_id=(spot_source.event_id if spot_source is not None else None),
            primary_spot_observed_at=(spot_source.observed_at if spot_source is not None else None),
            source_sequences={
                name: source.sequence
                for name, source in frame.source_manifest.items()
                if source.sequence is not None
            },
            content_hash=frame.content_hash,
        )


class CausalFrameHistory:
    """Stages a current frame without mutating the committed rolling window."""

    def __init__(
        self,
        experiment_id: UUID,
        symbols: Sequence[str],
        *,
        timeframe: str = "1m",
        maximum_bars: int = 240,
        pipeline: FeaturePipeline | None = None,
    ) -> None:
        normalized = tuple(symbols)
        if experiment_id.int == 0:
            raise ValueError("history experiment_id cannot be nil")
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("history symbols must be nonempty and unique")
        if any(not symbol or symbol != symbol.upper() for symbol in normalized):
            raise ValueError("history symbols must be uppercase")
        if not timeframe or maximum_bars < 60:
            raise ValueError("history requires a timeframe and at least 60 bars")
        self._experiment_id = experiment_id
        self._symbols = frozenset(normalized)
        self._timeframe = timeframe
        self._maximum_bars = maximum_bars
        self._pipeline = pipeline or FeaturePipeline()
        self._snapshots: dict[str, list[CommittedFrameSnapshot]] = {
            symbol: [] for symbol in normalized
        }

    def restore(self, snapshots: Sequence[CommittedFrameSnapshot]) -> None:
        if any(self._snapshots.values()):
            raise FrameHistoryConflict("history can only be restored before use")
        grouped: dict[str, list[CommittedFrameSnapshot]] = {symbol: [] for symbol in self._symbols}
        for snapshot in snapshots:
            self._validate_identity(snapshot.experiment_id, snapshot.symbol, snapshot.timeframe)
            grouped[snapshot.symbol].append(snapshot)
        for symbol, values in grouped.items():
            ordered = sorted(values, key=lambda item: item.bar.bar_close_at)
            for left, right in zip(ordered, ordered[1:]):
                if left.bar.bar_close_at != right.bar.bar_open_at:
                    raise FrameHistoryConflict(f"persisted history is not contiguous for {symbol}")
                if left.frame_id == right.frame_id or left.content_hash == right.content_hash:
                    raise FrameHistoryConflict(f"persisted history is duplicated for {symbol}")
            self._snapshots[symbol] = ordered[-self._maximum_bars :]

    def compute(self, frame: CausalMinuteFrame) -> FeatureSet | None:
        prior = self._prior(frame)
        candles = [self._kline(item.symbol, item.bar) for item in prior]
        candles.append(self._kline(frame.key.symbol, frame.bar))
        order_book = None
        book_source = frame.source_manifest.get("order_book")
        if frame.book_bids and frame.book_asks and book_source is not None:
            order_book = OrderBookSnapshot(
                symbol=frame.key.symbol,
                timestamp=book_source.observed_at,
                bids=[(level.price, level.quantity) for level in frame.book_bids],
                asks=[(level.price, level.quantity) for level in frame.book_asks],
                last_update_id=book_source.sequence or 0,
            )
        funding = None
        funding_source = frame.source_manifest.get("mark_funding")
        if (
            frame.funding_rate is not None
            and frame.mark_price is not None
            and funding_source is not None
        ):
            funding = FundingRateData(
                symbol=frame.key.symbol,
                funding_time=funding_source.venue_event_at,
                funding_rate=frame.funding_rate,
                mark_price=frame.mark_price,
            )
        return self._pipeline.compute(
            frame.key.symbol,
            candles,
            order_book=order_book,
            latest_funding=funding,
            timeframe=frame.key.timeframe,
        )

    def integrity_context(
        self,
        frame: CausalMinuteFrame,
        *,
        evaluated_at: datetime,
    ) -> IntegrityContext:
        prior = self._prior(frame)
        previous = prior[-1] if prior else None
        closes = [item.bar.close for item in prior]
        returns = tuple(
            (current - previous_close) / previous_close
            for previous_close, current in zip(closes, closes[1:])
        )
        return IntegrityContext(
            frame=frame,
            evaluated_at=evaluated_at,
            previous_bar_close_at=(previous.bar.bar_close_at if previous is not None else None),
            previous_close=(previous.bar.close if previous is not None else None),
            prior_sequences=(previous.source_sequences if previous is not None else {}),
            recent_close_returns=returns,
            historical_bar_count=len(prior),
        )

    def commit(self, frame: CausalMinuteFrame) -> bool:
        prior = self._prior(frame)
        values = self._snapshots[frame.key.symbol]
        if values and values[-1].bar.bar_close_at == frame.bar.bar_close_at:
            existing = values[-1]
            if existing.frame_id != frame.frame_id or existing.content_hash != frame.content_hash:
                raise FrameHistoryConflict("committed frame time has different content")
            return False
        if prior and prior[-1].bar.bar_close_at != frame.bar.bar_open_at:
            raise FrameHistoryConflict("committed frame does not continue causal history")
        values.append(CommittedFrameSnapshot.from_frame(frame))
        if len(values) > self._maximum_bars:
            del values[: len(values) - self._maximum_bars]
        return True

    def snapshots(self, symbol: str) -> tuple[CommittedFrameSnapshot, ...]:
        try:
            return tuple(self._snapshots[symbol])
        except KeyError as exc:
            raise ValueError(f"history symbol is not configured: {symbol}") from exc

    def close_series(self, symbol: str) -> tuple[tuple[datetime, Decimal], ...]:
        return tuple((item.bar.bar_close_at, item.bar.close) for item in self.snapshots(symbol))

    def benchmark_base(
        self,
        symbol: str,
        *,
        horizon_bars: int,
    ) -> CommittedFrameSnapshot | None:
        if horizon_bars < 2:
            raise ValueError("benchmark horizon must contain at least two bars")
        values = self.snapshots(symbol)
        if len(values) < horizon_bars:
            return None
        candidate = values[-horizon_bars]
        if candidate.primary_spot_price is None:
            return None
        return candidate

    def _prior(self, frame: CausalMinuteFrame) -> tuple[CommittedFrameSnapshot, ...]:
        self._validate_identity(
            frame.key.experiment_id,
            frame.key.symbol,
            frame.key.timeframe,
        )
        values = self._snapshots[frame.key.symbol]
        later = [item for item in values if item.bar.bar_close_at > frame.bar.bar_close_at]
        if later:
            raise FrameHistoryConflict("cannot evaluate a frame behind committed history")
        same = [item for item in values if item.bar.bar_close_at == frame.bar.bar_close_at]
        if same and (
            same[0].frame_id != frame.frame_id or same[0].content_hash != frame.content_hash
        ):
            raise FrameHistoryConflict("frame time has different committed content")
        return tuple(item for item in values if item.bar.bar_close_at < frame.bar.bar_close_at)

    def _validate_identity(self, experiment_id: UUID, symbol: str, timeframe: str) -> None:
        if (
            experiment_id != self._experiment_id
            or symbol not in self._symbols
            or timeframe != self._timeframe
        ):
            raise ValueError("frame history identity differs from runtime configuration")

    @staticmethod
    def _kline(symbol: str, bar: ClosedBarPayload) -> KlineData:
        return KlineData(
            symbol=symbol,
            timeframe=bar.timeframe,
            open_time=bar.bar_open_at,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            close_time=bar.bar_close_at,
            quote_volume=bar.quote_volume,
            trade_count=bar.trade_count,
            taker_buy_volume=bar.taker_buy_volume,
            taker_buy_quote_volume=bar.taker_buy_quote_volume,
            is_closed=bar.closed,
        )
