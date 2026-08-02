# MAAIS — System Constants
# All values extracted verbatim from BEHAVIOURS.md (MAAIS v21.1).
# Do not change without updating the source PDF and BEHAVIOURS.md.

# ── Rule 14 — Position Sizing ─────────────────────────────────────────────────
# Position Size = min(Half-Kelly Size, Volatility-Adjusted Size, Risk Cap)
MAX_RISK_PER_TRADE_PCT: float = 0.02  # 1–2% of total capital (upper bound)
MIN_RISK_PER_TRADE_PCT: float = 0.01  # 1–2% of total capital (lower bound)

# ── Rule 11 — Portfolio Risk Limit ───────────────────────────────────────────
PORTFOLIO_RISK_LIMIT_PCT: float = 0.15  # 10–15% total simultaneous exposure (upper)
PORTFOLIO_RISK_LIMIT_MIN_PCT: float = 0.10  # lower bound

# ── Rule 20 — Execution Constraints ──────────────────────────────────────────
MAX_LEVERAGE: int = 5  # system-wide leverage cap (confirmed Q25)

# ── Rule 6 — Z-Score Mean Reversion Trigger ──────────────────────────────────
ZSCORE_TRIGGER: float = 3.0  # |Z| > 3 signals mean reversion opportunity

# ── Rule 15 — Drawdown Risk Control ──────────────────────────────────────────
DRAWDOWN_NORMAL_THRESHOLD: float = 0.05  # below this → normal risk
DRAWDOWN_REDUCE_25_THRESHOLD: float = 0.05  # ≥ 5% → reduce position sizes 25%
DRAWDOWN_REDUCE_50_THRESHOLD: float = 0.10  # ≥ 10% → reduce position sizes 50%
DRAWDOWN_HALT_THRESHOLD: float = 0.20  # ≥ 20% → trading halt + system review

DRAWDOWN_REDUCE_25_FACTOR: float = 0.75  # multiply position size by this
DRAWDOWN_REDUCE_50_FACTOR: float = 0.50  # multiply position size by this

# ── Rule 12 — Correlation Control Thresholds ─────────────────────────────────
CORRELATION_FULL_THRESHOLD: float = 0.30  # < 0.30 → full allocation
CORRELATION_REDUCE_20_THRESHOLD: float = 0.60  # 0.30–0.60 → reduce 20%
CORRELATION_REDUCE_40_THRESHOLD: float = 0.70  # 0.60–0.70 → reduce 40%
# ≥ 0.70 → treat as correlated cluster

CORRELATION_REDUCE_20_FACTOR: float = 0.80  # multiply allocation by this
CORRELATION_REDUCE_40_FACTOR: float = 0.60  # multiply allocation by this


# ── Rule 13 — Market Regimes ──────────────────────────────────────────────────
class Regime:
    TRENDING = "trending"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"


ALL_REGIMES: tuple[str, ...] = (
    Regime.TRENDING,
    Regime.RANGE_BOUND,
    Regime.HIGH_VOLATILITY,
    Regime.LOW_VOLATILITY,
)

# ── Rule 22 — Capital Growth Phases ──────────────────────────────────────────
PHASE_1_MAX: float = 10_000  # < $10k   — Validation
PHASE_2_MAX: float = 50_000  # $10k–$50k — Diversification
PHASE_3_MAX: float = 250_000  # $50k–$250k — Portfolio optimization
# Phase 4: $250k+                  — Institutional scale


# ── Rule 4 — Agent Names (exactly 8) ─────────────────────────────────────────
class AgentName:
    MOMENTUM = "momentum"
    TECHNICAL_STRUCTURE = "technical_structure"
    LIQUIDITY = "liquidity"
    ORDER_FLOW_TOXICITY = "order_flow_toxicity"
    STOP_RUN_DETECTION = "stop_run_detection"
    MEAN_REVERSION = "mean_reversion"
    CARRY_YIELD = "carry_yield"
    MACRO_SENTIMENT = "macro_sentiment"


ALL_AGENTS: tuple[str, ...] = (
    AgentName.MOMENTUM,
    AgentName.TECHNICAL_STRUCTURE,
    AgentName.LIQUIDITY,
    AgentName.ORDER_FLOW_TOXICITY,
    AgentName.STOP_RUN_DETECTION,
    AgentName.MEAN_REVERSION,
    AgentName.CARRY_YIELD,
    AgentName.MACRO_SENTIMENT,
)

# ── Batch 3: Feature Engineering ─────────────────────────────────────────────
# Defaults chosen using industry-standard values.
# Change here to tune; no other files need updating.

ZSCORE_LOOKBACK: int = 20  # periods for Z-score mean/std calculation
CORRELATION_ROLLING_WINDOW: int = 60  # rolling window for pairwise correlation (Rule 12)
EMA_FAST_PERIOD: int = 9  # fast EMA (signal-line style)
EMA_SLOW_PERIOD: int = 21  # slow EMA (trend baseline)
ATR_PERIOD: int = 14  # Average True Range period
MOMENTUM_ROC_SHORT: int = 5  # short Rate-of-Change lookback
MOMENTUM_ROC_LONG: int = 20  # long Rate-of-Change lookback
REGIME_LOOKBACK: int = 50  # periods for regime volatility baseline
REGIME_TREND_THRESHOLD: float = 0.005  # |EMA_fast − EMA_slow| / EMA_slow > 0.5% → Trending
HIGH_VOL_MULTIPLIER: float = 1.5  # rolling_std > 1.5 × baseline → High Volatility
LOW_VOL_MULTIPLIER: float = 0.5  # rolling_std < 0.5 × baseline → Low Volatility
FUNDING_BIAS_THRESHOLD: float = 0.0001  # |rate| > 0.01% per 8h → directional bias
FUNDING_ANNUAL_MULTIPLIER: int = 3 * 365  # funding paid 3×/day × 365 days

# ── Batch 1: Market Data ─────────────────────────────────────────────────────

# MATICUSDT rebranded to POLUSDT on Binance Futures (Polygon: MATIC → POL, 2024).
# Verify the active contract name on your Binance account before going live.
TRADING_PAIRS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "POLUSDT",  # formerly MATICUSDT
)

PRIMARY_TIMEFRAME: str = "1m"
AGGREGATION_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "1h")
ALL_TIMEFRAMES: tuple[str, ...] = (PRIMARY_TIMEFRAME,) + AGGREGATION_TIMEFRAMES

# Minutes per timeframe — used by aggregator and validator
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}

HISTORICAL_DEPTH_YEARS: int = 3  # minimum; fetcher will attempt 5 if available

# Kafka topic names
KAFKA_TOPIC_KLINES: str = "maais.klines"
KAFKA_TOPIC_ORDERBOOK: str = "maais.orderbook"
KAFKA_TOPIC_TRADES: str = "maais.trades"
KAFKA_TOPIC_FUNDING: str = "maais.funding"

# Data integrity thresholds (Rule 16)
PRICE_OUTLIER_Z_THRESHOLD: float = 5.0  # |Z| > 5 on price change → outlier
CROSS_EXCHANGE_MAX_DIVERGENCE: float = 0.02  # 2% max futures/spot divergence
API_OUTAGE_TIMEOUT_SECONDS: int = 60  # no data for 60s → outage alert


# ── Rule 21 — Strategy Lifecycle Stages ──────────────────────────────────────
class StrategyStage:
    RESEARCH = "research"
    SIMULATION = "simulation"
    PILOT = "pilot"
    FULL_PRODUCTION = "full_production"
