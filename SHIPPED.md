# SHIPPED.md — What Has Been Built

> Tracks all completed and deployed components of the MAAIS system.
> Updated after each completed batch in the development process.

---

## STATUS: BATCH 0 COMPLETE

---

## TEMPLATE — Entry Format

When a component is shipped, add an entry using this format:

```
### [Component Name]
- **Batch:** B1 / B2 / etc.
- **Date shipped:** YYYY-MM-DD
- **Description:** What was built
- **Files:** List of files created or modified
- **Tests:** Yes / No / Partial
- **Notes:** Any relevant context
```

---

## SHIPPED COMPONENTS

### Batch 0 — Project Foundation
- **Batch:** B0
- **Date shipped:** 2026-03-12
- **Description:** Full Python project skeleton matching the 7-layer architecture. No business logic — foundation only.
- **Files created:**
  - `maais/__init__.py` + all 7 layer packages (`market_data/`, `feature_pipeline/`, `agents/`, `decision/`, `risk/`, `execution/`, `monitoring/`)
  - `maais/config/constants.py` — all thresholds from BEHAVIOURS.md (22 rules)
  - `maais/config/settings.py` — pydantic-settings `.env` loader
  - `maais/db/connection.py` — SQLAlchemy 2.0 async engine + session factory
  - `maais/core/logging.py` — structlog setup (JSON in prod, console in dev)
  - `alembic/` — migration scaffold + `0001_baseline.py` (empty starting point)
  - `alembic.ini`
  - `.env.template`
  - `tests/conftest.py` + `tests/test_config.py`
  - `data/` directory (DuckDB files stored here, gitignored)
- **pyproject.toml:** Updated with full dependency set (SQLAlchemy, Alembic, psycopg3, DuckDB, Kafka, pydantic-settings, structlog, pytest)
- **Tests:** 11/11 passing
- **Architecture decisions locked:**
  - Kafka for real-time Binance WebSocket streaming (wired in Batch 1)
  - DuckDB for analytical queries and feature store (wired in Batch 3)
  - PostgreSQL for trade records, compliance, strategy registry, news articles (JSONB)
  - Hybrid agents: algorithmic core + Ollama LLM at Decision Engine adversarial step (Batch 5)
  - Iceberg + Nessie deferred to Batch 10+

---

## DOCUMENTATION & SETUP

| Item | Date | Notes |
|------|------|-------|
| `ROADMAP.md` | 2026-03-12 | Created from maais.pdf v19.0 |
| `SHIPPED.md` | 2026-03-12 | This file |
| `PLAN.md` | 2026-03-12 | Short-term plan |
| `BEHAVIOURS.md` | 2026-03-12 | System constraints from PDF |
| `ORCHESTRATOR.md` | 2026-03-12 | Development process orchestration |

---

### Batch 1 — Market Data Layer
- **Batch:** B1
- **Date shipped:** 2026-03-12
- **Description:** Full Binance USDT-Perp Futures data pipeline. Historical ingestion + live WebSocket streaming + Kafka publishing + Rule 16 integrity validation.
- **Files created:**
  - `maais/market_data/schemas.py` — dataclasses: KlineData, FundingRateData, OrderBookSnapshot, TradeData
  - `maais/market_data/models.py` — SQLAlchemy ORM: Kline, FundingRate, OrderBookSnapshot
  - `maais/market_data/connectors/binance_rest.py` — REST connector (klines, funding rates, bulk CSV download from data.binance.vision)
  - `maais/market_data/connectors/binance_websocket.py` — WebSocket connector (1m klines, depth, aggTrade, markPrice) with reconnection backoff
  - `maais/market_data/integrity/validator.py` — Rule 16: 6 validation checks
  - `maais/market_data/aggregator.py` — 1m → 5m/15m/1h OHLCV aggregation
  - `maais/market_data/kafka_producer.py` — Kafka producer for all 4 topics
  - `maais/market_data/ingestor.py` — Historical ingestion orchestrator (bulk + REST fill + DuckDB + PostgreSQL)
  - `alembic/versions/0002_market_data_tables.py` — klines, funding_rates, order_book_snapshots tables
  - `tests/test_market_data_aggregator.py` + `tests/test_market_data_validator.py`
- **Tests:** 46/46 passing
- **Confirmed decisions:**
  - 10 pairs: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, ADAUSDT, DOGEUSDT, LINKUSDT, AVAXUSDT, POLUSDT
  - 1m primary timeframe, aggregated to 5m/15m/1h
  - 3-year minimum historical depth (5 years attempted)
  - Bulk download via data.binance.vision; REST API fills gaps
  - DuckDB for analytical store; PostgreSQL for canonical record

### Batch 2 — Compliance Engine
- **Batch:** B2
- **Date shipped:** 2026-03-12
- **Description:** Full Rule 8 trade record (11 fields) + Rule 9 FIFO ledger + exchange rate fetcher + post-trade reasoning storage.
- **Files created:**
  - `maais/compliance/schemas.py` — LotData, TradeRecordData (11 fields), PostTradeReasoningData
  - `maais/compliance/models.py` — SQLAlchemy ORM: TradeLot, TradeRecord, PostTradeReasoning
  - `maais/compliance/fifo_ledger.py` — FIFO ledger with open/close/partial-close/load
  - `maais/compliance/trade_logger.py` — async PostgreSQL persistence for all compliance records
  - `maais/compliance/exchange_rate.py` — USDT → local currency rate fetcher (Binance spot)
  - `alembic/versions/0003_compliance_tables.py` — trade_lots, trade_records, post_trade_reasoning
  - `tests/test_compliance_fifo.py`
- **Tests:** 63/63 passing (all batches combined)

### Batch 3 — Feature Engineering Pipeline
- **Batch:** B3
- **Date shipped:** 2026-03-12
- **Description:** All signal features consumed by the 8 analytical agents. Pure computation — no external I/O.
- **Files created:**
  - `maais/feature_pipeline/features.py` — FeatureSet dataclass (all features)
  - `maais/feature_pipeline/zscore.py` — Rule 6 Z-score (lookback=20, trigger=3.0)
  - `maais/feature_pipeline/momentum.py` — EMA (fast=9, slow=21), ROC (5, 20)
  - `maais/feature_pipeline/volatility.py` — ATR (14), rolling std of returns
  - `maais/feature_pipeline/funding_features.py` — rate, annualized, bias classification
  - `maais/feature_pipeline/orderbook_features.py` — spread, book imbalance
  - `maais/feature_pipeline/regime_classifier.py` — Rule 13: 4 regimes, volatility-first priority
  - `maais/feature_pipeline/feature_store.py` — DuckDB persistence
  - `maais/feature_pipeline/pipeline.py` — `FeaturePipeline.compute()` + `compute_batch()`
  - `tests/test_feature_pipeline.py`
- **Tests:** 107/107 passing
- **Open questions resolved:** Z-score lookback=20, moving average type=EMA (both configurable in constants.py)

### Batch 4 — Strategy Agent Engine
- **Batch:** B4
- **Date shipped:** 2026-03-13
- **Description:** All 8 analytical agents producing 4 outputs each (Rule 3). Regime compatibility map (Rule 13). Parallel async runner via asyncio.gather.
- **Files created:**
  - `maais/agents/base.py` — AgentOutput (4 validated fields), BaseAgent ABC, _neutral/_clip/_votes_to_probability/_signal_to_output helpers
  - `maais/agents/regime_gate.py` — filter_compatible_agents()
  - `maais/agents/mean_reversion.py` — |Z| > 3 trigger; compatible: RANGE_BOUND + LOW_VOLATILITY
  - `maais/agents/momentum.py` — EMA crossover + ROC votes; compatible: TRENDING
  - `maais/agents/technical_structure.py` — EMA alignment; compatible: TRENDING + RANGE_BOUND
  - `maais/agents/liquidity.py` — book imbalance + spread; compatible: all
  - `maais/agents/order_flow_toxicity.py` — spread × imbalance toxicity; compatible: all
  - `maais/agents/stop_run_detection.py` — Z > 4 + vol; compatible: HIGH_VOLATILITY + TRENDING
  - `maais/agents/carry_yield.py` — funding rate bias; compatible: all
  - `maais/agents/macro_sentiment.py` — regime proxy logic; compatible: all
  - `maais/agents/runner.py` — build_agent_registry() + run_agents() via asyncio.gather
  - `tests/test_agents.py`
- **Tests:** 81/81 passing (188/188 total)

### Batch 5 — Decision Engine
- **Batch:** B5
- **Date shipped:** 2026-03-13
- **Description:** Weighted consensus + adversarial protocol (Rule 5) + EV engine (Rule 1) + transaction cost estimator + trade gate.
- **Files created:**
  - `maais/decision/schemas.py` — ConsensusResult, AdversarialSummary, CostEstimate, EVResult, DecisionResult
  - `maais/decision/weights.py` — AgentWeightRegistry (equal init 1.0, updatable by Batch 9)
  - `maais/decision/consensus.py` — weighted aggregation of AgentOutputs
  - `maais/decision/adversarial.py` — minority challenge; blocks trade if dissenter confidence ≥ 0.65
  - `maais/decision/cost_estimator.py` — round-trip fee (0.08%) + ATR/spread slippage model
  - `maais/decision/ev_engine.py` — EV = P(win)×gain − P(loss)×loss + funding_carry − costs
  - `maais/decision/gate.py` — trade gate (EV > 0) + alpha validator (Rule 2)
  - `maais/decision/engine.py` — DecisionEngine.evaluate() orchestrating full pipeline
  - `alembic/versions/0004_agent_weights.py` — agent_weights table, seeded 1.0 for all 8
  - `tests/test_decision.py`
- **Tests:** 49/49 passing (237/237 total)
- **Architecture decisions:**
  - Ollama LLM stub interface defined in adversarial.py (llm_reasoning field); wired in Batch 5 when needed
  - Equal weights (1.0) for all agents; Batch 9 updates via update_weight()

### Batch 6 — Risk Engine
- **Batch:** B6
- **Date shipped:** 2026-03-13
- **Description:** Half-Kelly sizing, volatility normalization, drawdown controls (Rule 15), correlation enforcement (Rule 12), portfolio cap (Rule 11). Final size = min(Half-Kelly, Vol-Adjusted, Risk Cap) × drawdown_mult × correlation_mult.
- **Files created:**
  - `maais/risk/schemas.py` — PositionSize, DrawdownState dataclasses
  - `maais/risk/kelly.py` — half_kelly_fraction(), volatility_adjusted_fraction()
  - `maais/risk/drawdown.py` — DrawdownController: tracks peak, computes multiplier (1.0/0.75/0.50/0.0)
  - `maais/risk/correlation.py` — CorrelationController: rolling 60-period Pearson; multipliers 1.0/0.80/0.60/0.0
  - `maais/risk/portfolio.py` — PortfolioRiskLimiter: 15% total cap, can_add_position()
  - `maais/risk/engine.py` — RiskEngine.evaluate() applying full Rule 14 pipeline
  - `tests/test_risk.py`
- **Tests:** 56/56 passing (293/293 total)
- **Constants added:** `CORRELATION_ROLLING_WINDOW = 60` in config/constants.py

### Batch 7 — Execution Engine
- **Batch:** B7
- **Date shipped:** 2026-03-13
- **Description:** Binance Futures order placement (market/limit/stop-limit), fill tracking, funding payment recording, leverage enforcement (5× cap, Rule 20), cost overrun detection, and full Rule 8 compliance recording on fill.
- **Files created:**
  - `maais/execution/schemas.py` — OrderType, OrderSide, OrderStatus, OrderRequest, FillRecord, OrderResult
  - `maais/execution/leverage.py` — LeverageEnforcer: cap at 5×, caches per-symbol set state
  - `maais/execution/binance_client.py` — async HMAC-signed Binance Futures REST client
  - `maais/execution/fill_tracker.py` — exponential-backoff poll (0.5→8s, 5 attempts)
  - `maais/execution/funding_tracker.py` — fetches and caches cumulative funding per symbol
  - `maais/execution/trade_record_writer.py` — record_open() + record_close() via FifoLedger + TradeLogger
  - `maais/execution/engine.py` — ExecutionEngine: 6-step pipeline with Rule 20 safeguards
  - `tests/test_execution.py`
- **Tests:** 30/30 passing (323/323 total)
- **Constants added:** `MAX_LEVERAGE = 5` in config/constants.py
- **Bug fixed:** BUY→"long"/SELL→"short" mapping in trade_record_writer.py (FifoLedger requires "long"/"short" not "buy"/"sell")

### Batch 8 — Monitoring Layer
- **Batch:** B8
- **Date shipped:** 2026-03-13
- **Description:** Full observability, tiered alerting (structlog + Telegram), kill-switch (Rule 17), black swan protection (Rule 18), drawdown monitoring with auto-halt, component health tracking, human override prevention (Rule 20).
- **Files created:**
  - `maais/monitoring/schemas.py` — AlertLevel, ComponentName, AlertEvent, HealthStatus
  - `maais/monitoring/alerting.py` — AlertDispatcher: structlog always + Telegram when configured
  - `maais/monitoring/health.py` — SystemHealthTracker: ping/error/staleness (60s threshold)
  - `maais/monitoring/kill_switch.py` — KillSwitch: Rule 17 halt; reset requires "MANUAL_RESET_CONFIRMED" (Rule 20)
  - `maais/monitoring/black_swan.py` — BlackSwanGuard: Rule 18; vol circuit breaker + liquidity collapse + exposure ceiling
  - `maais/monitoring/drawdown_monitor.py` — DrawdownMonitor: tiered alerts at 5%/10%/15%/20%; triggers kill-switch at halt
  - `maais/monitoring/engine.py` — MonitoringEngine: is_trading_allowed() runs all checks in order
  - `tests/test_monitoring.py`
- **Files updated:** `.env.template` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), `config/settings.py` (telegram fields)
- **Tests:** 51/51 passing (374/374 total)

*Last updated: 2026-03-13*
