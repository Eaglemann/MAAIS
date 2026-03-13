"""Feature Engineering Pipeline — orchestrates all feature calculators.

Entry point for Batch 4 agents. Each agent calls:
    features = pipeline.compute(symbol, candles, order_book, funding)

The pipeline produces one FeatureSet per call, optionally saving to DuckDB.
"""

from maais.config.constants import PRIMARY_TIMEFRAME
from maais.feature_pipeline.features import FeatureSet
from maais.feature_pipeline.feature_store import FeatureStore
from maais.feature_pipeline.funding_features import compute_funding_features
from maais.feature_pipeline.momentum import compute_momentum_features
from maais.feature_pipeline.orderbook_features import compute_orderbook_features
from maais.feature_pipeline.regime_classifier import classify_regime
from maais.feature_pipeline.volatility import compute_volatility_features
from maais.feature_pipeline.zscore import compute_zscore
from maais.market_data.schemas import FundingRateData, KlineData, OrderBookSnapshot


class FeaturePipeline:
    """Stateless feature computation pipeline."""

    def __init__(self, store: FeatureStore | None = None) -> None:
        self._store = store  # optional — pass None to skip persistence

    def compute(
        self,
        symbol: str,
        candles: list[KlineData],
        order_book: OrderBookSnapshot | None = None,
        latest_funding: FundingRateData | None = None,
        timeframe: str = PRIMARY_TIMEFRAME,
    ) -> FeatureSet | None:
        """Compute all features for the latest candle in the series.

        Args:
            symbol: Trading pair (e.g. "BTCUSDT").
            candles: Historical candles, ascending by open_time, same timeframe.
            order_book: Latest order book snapshot (optional).
            latest_funding: Most recent funding rate (optional).
            timeframe: Candle timeframe label.

        Returns:
            FeatureSet, or None if insufficient candles.
        """
        if not candles:
            return None

        closes = [float(c.close) for c in candles]

        # Z-score
        zscore, zscore_mean, zscore_std = compute_zscore(closes)

        # Momentum
        mom = compute_momentum_features(closes)

        # Volatility
        vol = compute_volatility_features(candles)

        # Order book
        ob = compute_orderbook_features(order_book)

        # Funding
        fund = compute_funding_features(latest_funding)

        # Regime
        regime = classify_regime(closes, vol["rolling_std"], mom["ema_fast"], mom["ema_slow"])

        return FeatureSet(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=candles[-1].close_time,
            zscore=zscore,
            zscore_mean=zscore_mean,
            zscore_std=zscore_std,
            ema_fast=mom["ema_fast"],
            ema_slow=mom["ema_slow"],
            ema_signal=mom["ema_signal"],
            roc_short=mom["roc_short"],
            roc_long=mom["roc_long"],
            atr=vol["atr"],
            rolling_std=vol["rolling_std"],
            bid_ask_spread=ob["bid_ask_spread"],
            book_imbalance=ob["book_imbalance"],
            funding_rate=fund["funding_rate"],
            annualized_funding=fund["annualized_funding"],
            funding_bias=fund["funding_bias"],
            regime=regime,
        )

    def compute_batch(
        self,
        symbol: str,
        candles: list[KlineData],
        timeframe: str = PRIMARY_TIMEFRAME,
    ) -> list[FeatureSet]:
        """Compute features for every candle in the series (rolling window).

        Useful for backtesting and historical feature generation.
        """
        results: list[FeatureSet] = []
        for i in range(1, len(candles) + 1):
            fs = self.compute(symbol, candles[:i], timeframe=timeframe)
            if fs is not None:
                results.append(fs)

        if self._store and results:
            self._store.save(results)

        return results
