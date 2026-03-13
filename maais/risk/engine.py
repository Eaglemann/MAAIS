"""Risk Engine — orchestrates all position sizing and risk controls.

Pipeline (per trade decision):
  1. Half-Kelly fraction
  2. Volatility-adjusted fraction
  3. Risk cap (1–2% per trade, Rule 14)
  4. base_fraction = min(half_kelly, vol_adjusted, risk_cap)
  5. drawdown_multiplier (Rule 15)
  6. correlation_multiplier (Rule 12) — checked against all open positions
  7. final_fraction = base × drawdown × min(correlation multipliers)
  8. Portfolio cap check (Rule 11)
  9. Return RiskResult

Final size formula (Rule 14):
  Position Size = min(Half-Kelly Size, Volatility-Adjusted Size, Risk Cap)
"""

from maais.config.constants import MAX_RISK_PER_TRADE_PCT
from maais.core.logging import get_logger
from maais.decision.schemas import EVResult
from maais.feature_pipeline.features import FeatureSet
from maais.risk.correlation import CorrelationController
from maais.risk.drawdown import DrawdownController
from maais.risk.kelly import half_kelly_fraction, volatility_adjusted_fraction
from maais.risk.portfolio import PortfolioRiskLimiter
from maais.risk.schemas import PositionSize

logger = get_logger(__name__)

# Hard risk cap per trade (Rule 14): default to midpoint of 1–2% range
_DEFAULT_RISK_CAP_PCT = MAX_RISK_PER_TRADE_PCT   # 2% absolute maximum


class RiskEngine:
    """Evaluates position size for a proposed trade, applying all risk controls."""

    def __init__(
        self,
        drawdown_controller: DrawdownController,
        correlation_controller: CorrelationController,
        portfolio_limiter: PortfolioRiskLimiter,
        risk_cap_pct: float = _DEFAULT_RISK_CAP_PCT,
    ) -> None:
        self.drawdown = drawdown_controller
        self.correlation = correlation_controller
        self.portfolio = portfolio_limiter
        self._risk_cap_pct = risk_cap_pct

    def evaluate(
        self,
        symbol: str,
        capital: float,
        ev: EVResult,
        features: FeatureSet,
        open_symbols: list[str] | None = None,
    ) -> PositionSize:
        """Compute the final approved position size.

        Args:
            symbol: Symbol being considered.
            capital: Total account capital in USD.
            ev: EVResult from Decision Engine (p_win, gain, loss).
            features: Current FeatureSet (ATR, price proxy via zscore_mean).
            open_symbols: Currently open position symbols for correlation check.

        Returns:
            PositionSize — approved=False means risk controls blocked the trade.
        """
        open_symbols = open_symbols or []

        # 1. Half-Kelly
        hk = half_kelly_fraction(ev.p_win, ev.expected_gain_pct, ev.expected_loss_pct)

        # 2. Volatility-adjusted
        price = features.zscore_mean   # best price proxy from feature set
        va = volatility_adjusted_fraction(features.atr, price)

        # 3. Risk cap
        risk_cap = self._risk_cap_pct

        # 4. Base fraction = min of all three (Rule 14)
        base_fraction = min(hk, va, risk_cap)

        # 5. Drawdown multiplier (Rule 15)
        dd_state = self.drawdown.get_state()
        dd_multiplier = dd_state.multiplier

        if dd_state.is_halted:
            return PositionSize(
                symbol=symbol,
                capital=capital,
                half_kelly_fraction=hk,
                vol_adjusted_fraction=va,
                risk_cap_fraction=risk_cap,
                drawdown_multiplier=0.0,
                correlation_multiplier=1.0,
                base_fraction=base_fraction,
                final_fraction=0.0,
                final_usd=0.0,
                approved=False,
                rejection_reason=f"drawdown_halt: drawdown={dd_state.drawdown_pct:.2%}",
            )

        # 6. Correlation multiplier (Rule 12) — worst-case across all open positions
        corr_multiplier = 1.0
        for open_sym in open_symbols:
            if open_sym == symbol:
                continue
            m = self.correlation.get_multiplier(symbol, open_sym)
            corr_multiplier = min(corr_multiplier, m)

        if corr_multiplier == 0.0:
            return PositionSize(
                symbol=symbol,
                capital=capital,
                half_kelly_fraction=hk,
                vol_adjusted_fraction=va,
                risk_cap_fraction=risk_cap,
                drawdown_multiplier=dd_multiplier,
                correlation_multiplier=0.0,
                base_fraction=base_fraction,
                final_fraction=0.0,
                final_usd=0.0,
                approved=False,
                rejection_reason=f"correlation_cluster: symbol={symbol} highly correlated with open positions",
            )

        # 7. Final fraction and USD size
        final_fraction = base_fraction * dd_multiplier * corr_multiplier
        final_usd = final_fraction * capital

        # 8. Portfolio cap check (Rule 11)
        allowed, portfolio_reason = self.portfolio.can_add_position(symbol, final_usd, capital)
        if not allowed:
            return PositionSize(
                symbol=symbol,
                capital=capital,
                half_kelly_fraction=hk,
                vol_adjusted_fraction=va,
                risk_cap_fraction=risk_cap,
                drawdown_multiplier=dd_multiplier,
                correlation_multiplier=corr_multiplier,
                base_fraction=base_fraction,
                final_fraction=final_fraction,
                final_usd=final_usd,
                approved=False,
                rejection_reason=portfolio_reason,
            )

        logger.info(
            "position_sized",
            symbol=symbol,
            half_kelly=f"{hk:.4f}",
            vol_adjusted=f"{va:.4f}",
            risk_cap=f"{risk_cap:.4f}",
            base=f"{base_fraction:.4f}",
            dd_mult=dd_multiplier,
            corr_mult=corr_multiplier,
            final_pct=f"{final_fraction:.4f}",
            final_usd=f"{final_usd:.2f}",
        )

        return PositionSize(
            symbol=symbol,
            capital=capital,
            half_kelly_fraction=hk,
            vol_adjusted_fraction=va,
            risk_cap_fraction=risk_cap,
            drawdown_multiplier=dd_multiplier,
            correlation_multiplier=corr_multiplier,
            base_fraction=base_fraction,
            final_fraction=final_fraction,
            final_usd=final_usd,
            approved=True,
            rejection_reason=None,
        )
