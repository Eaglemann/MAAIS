"""Black swan protection — Rule 18 (BEHAVIOURS.md).

Three circuit breakers:
  1. Volatility circuit breaker: suspend if rolling_std > HIGH_VOL_MULTIPLIER × baseline_std
  2. Liquidity collapse: suspend if bid_ask_spread > LIQUIDITY_COLLAPSE_SPREAD_THRESHOLD
  3. Exposure limit: emergency flag if total portfolio exposure > hard ceiling

All suspensions fire alerts. The MonitoringEngine checks these before allowing trades.
"""

from __future__ import annotations

from maais.config.constants import HIGH_VOL_MULTIPLIER
from maais.core.logging import get_logger
from maais.feature_pipeline.features import FeatureSet

logger = get_logger(__name__)

_LIQUIDITY_COLLAPSE_SPREAD = 0.05
_EXPOSURE_HARD_CEILING = 0.25


class BlackSwanGuard:
    """Checks for black swan conditions before allowing a trade."""

    def __init__(
        self,
        baseline_std: float = 0.002,
        vol_multiplier: float = HIGH_VOL_MULTIPLIER,
        liquidity_spread_threshold: float = _LIQUIDITY_COLLAPSE_SPREAD,
        exposure_ceiling: float = _EXPOSURE_HARD_CEILING,
    ) -> None:
        self._baseline_std = baseline_std
        self._vol_multiplier = vol_multiplier
        self._liquidity_threshold = liquidity_spread_threshold
        self._exposure_ceiling = exposure_ceiling

    def check(
        self,
        features: FeatureSet,
        total_exposure_pct: float = 0.0,
    ) -> tuple[bool, str | None]:
        """Run all black swan checks. Returns (allowed, reason)."""
        if features.rolling_std is not None:
            vol_threshold = self._baseline_std * self._vol_multiplier
            if features.rolling_std > vol_threshold:
                reason = (
                    f"volatility_circuit_breaker: "
                    f"rolling_std={features.rolling_std:.6f} > "
                    f"threshold={vol_threshold:.6f}"
                )
                logger.warning(
                    "black_swan_vol",
                    symbol=features.symbol,
                    rolling_std=features.rolling_std,
                    threshold=vol_threshold,
                )
                return False, reason

        if features.bid_ask_spread is not None:
            if features.bid_ask_spread > self._liquidity_threshold:
                reason = (
                    f"liquidity_collapse: "
                    f"spread={features.bid_ask_spread:.4f} > "
                    f"threshold={self._liquidity_threshold:.4f}"
                )
                logger.warning(
                    "black_swan_liquidity",
                    symbol=features.symbol,
                    spread=features.bid_ask_spread,
                    threshold=self._liquidity_threshold,
                )
                return False, reason

        if total_exposure_pct > self._exposure_ceiling:
            reason = (
                f"exposure_ceiling: "
                f"exposure={total_exposure_pct:.2%} > "
                f"ceiling={self._exposure_ceiling:.2%}"
            )
            logger.warning(
                "black_swan_exposure", exposure=total_exposure_pct, ceiling=self._exposure_ceiling
            )
            return False, reason

        return True, None

    @property
    def baseline_std(self) -> float:
        return self._baseline_std

    @property
    def vol_multiplier(self) -> float:
        return self._vol_multiplier

    @property
    def liquidity_threshold(self) -> float:
        return self._liquidity_threshold

    @property
    def exposure_ceiling(self) -> float:
        return self._exposure_ceiling
