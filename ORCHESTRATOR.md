# ORCHESTRATOR.md — Historical Development Record

> **HISTORICAL ONLY — DO NOT USE THIS FILE AS RUNTIME OR IMPLEMENTATION
> INSTRUCTIONS.** Historical live-money and production-order text below is
> superseded. MAAIS supports replay, public-data local paper trading, and optional
> Binance Demo/Testnet protocol smoke tests only; it has no live-money mode. Use
> `AGENTS.md`, `README.md`, and `docs/runbooks/` for current instructions.

## CURRENT AUTHORITY

The historical batches in this file have been superseded by the implemented
paper platform and its exact-commit qualification, recovery, process-drill,
24-hour soak, and seven-day preflight gates. This record authorizes no run.

---

## CORE PRINCIPLE

Built in **small, deliberate batches**. Each batch:
1. Proposed by me
2. Reviewed and approved by you
3. Built
4. Tested
5. Logged in `SHIPPED.md`

**No batch starts without your explicit go-ahead.**

---

## RULES OF ENGAGEMENT

| Rule | Description |
|------|-------------|
| **Ask, never assume** | Anything unclear gets asked before code is written |
| **One batch at a time** | Complete and confirm before proposing the next |
| **No scope creep** | Only build what is in the approved scope |
| **BEHAVIOURS.md is sacred** | All 22 rules are non-negotiable |
| **SHIPPED.md updated immediately** | After every batch, before anything else |
| **PLAN.md reflects current reality** | Updated at start of each batch |

---

## BATCH STRUCTURE

```
PROPOSE → APPROVE → BUILD → TEST → SHIP → LOG → NEXT
```

---

## BATCH MAP

> Based on MAAIS v21.1 — 7-layer architecture from Section 23.
> Batch order maps directly to the layers, then adds cross-cutting concerns.

---

### BATCH 0 — Project Foundation

**Goal:** Python project skeleton matching the 7-layer architecture.

**All decisions confirmed — no blockers.**

**Tasks:**
- Directory structure: `market_data/`, `feature_pipeline/`, `agents/`, `decision/`, `risk/`, `execution/`, `monitoring/`
- `pyproject.toml` + dependency management (uv)
- `.env` template (Binance API keys, DB connection)
- PostgreSQL connection + base migration
- Logging configuration
- pytest scaffold with base fixtures
- `config/` for all threshold constants (drawdown levels, correlation thresholds, risk caps, etc.)

**Dependencies:** None.

---

### BATCH 1 — Market Data Layer (Layer 1)

**Goal:** Binance USDT-Perpetual Futures data ingestion with full data integrity validation.

**Questions to answer before this batch:**
- What data granularity? (1m / 5m / 1h / daily)
- Historical depth required?
- Which USDT-perp pairs to start with?

**Tasks:**
- Binance WebSocket connector (live order book, trades, funding rates)
- Binance REST connector (historical klines + funding rate history)
- Data normalization layer
- **Data Integrity Layer** (Rule 16): missing-data detection, price-outlier filtering, cross-exchange comparison, timestamp synchronization, API outage detection, historical dataset validation
- PostgreSQL tables for market data

**Dependencies:** Batch 0

---

### BATCH 2 — Compliance Engine (Trade Record)

**Goal:** The mandatory 11-field trade record with FIFO ledger.

> Non-negotiable — Rules 8 and 9.

**Tasks:**
- Trade record schema: Trade ID, Timestamp, Asset, Entry Price, Exit Price, Position Size, Fees, Funding Paid/Received, Profit/Loss, Exchange Rate, Strategy ID
- FIFO ledger engine
- Trade log persistence (PostgreSQL)
- Exchange rate fetcher at execution time
- Post-trade reasoning record storage

**Dependencies:** Batch 0

---

### BATCH 3 — Feature Engineering Pipeline (Layer 2)

**Goal:** Compute all signals and features each agent needs.

**Questions to answer before this batch:**
- Lookback window for Z-score mean calculation?
- Moving average type (SMA / EMA)?

**Tasks:**
- Z-score calculator: `Z = (Price − Mean) / Standard Deviation` (trigger at |Z| > 3 — Rule 6)
- Funding rate feature extractor
- Momentum indicators
- Order book depth and imbalance features
- Volatility features (ATR / rolling std dev)
- Regime classifier (Trending / Range-Bound / High Volatility / Low Volatility — Rule 13)
- Feature store (PostgreSQL or in-memory)

**Dependencies:** Batch 1

---

### BATCH 4 — Strategy Agent Engine (Layer 3)

**Goal:** All 8 analytical agents producing 4 outputs each.

**Questions to answer before this batch:**
- Parallel or sequential agent execution?

**Tasks:**
- Base agent abstract class (outputs: directional hypothesis, probability estimate, confidence score, risk estimate — Rule 3)
- Momentum Agent
- Technical Structure Agent
- Liquidity Agent
- Order Flow Toxicity Agent
- Stop-Run Detection Agent
- Mean Reversion Agent (uses Z-score, |Z| > 3)
- Carry Yield Agent (uses funding rates)
- Macro Sentiment Agent
- Strategy-to-regime compatibility map (Rule 13: strategies activate only in compatible regimes)

**Dependencies:** Batch 3

---

### BATCH 5 — Decision Engine (Layer 4)

**Goal:** Adversarial protocol + weighted EV scoring + trade gate.

**Questions to answer before this batch:**
- How are agent weights initialized?
- How are weights updated over time (ties to Batch 9)?

**Tasks:**
- Adversarial reasoning process (agents challenge each other — Rule 5)
- Final engine evaluates: probability consensus, confidence weighting, expected value (Rule 5)
- EV engine: `EV = P(win) × Gain − P(loss) × Loss` + funding-rate carry (Rule 1)
- Transaction cost estimator (exchange fees, slippage, spreads)
- Slippage model: f(order size, volatility, liquidity)
- Trade gate: EV > 0 after all costs (Rule 1)
- Alpha validator: strategy return > market return after costs (Rule 2)

**Dependencies:** Batch 4

---

### BATCH 6 — Risk Engine (Layer 5)

**Goal:** Half-Kelly sizing, drawdown controls, portfolio limits, correlation enforcement.

> All values fully specified — no open questions.

**Tasks:**
- Half-Kelly calculator
- Volatility normalization (size inversely proportional to volatility)
- Risk cap enforcer (1–2% per trade — Rule 14)
- Final size: `Position Size = min(Half-Kelly Size, Volatility-Adjusted Size, Risk Cap)` (Rule 14)
- Portfolio risk limiter: 10–15% total simultaneous exposure (Rule 11)
- **Drawdown risk control** (Rule 15):
  - < 5% → normal risk
  - 5–10% → reduce position sizes 25%
  - 10–15% → reduce position sizes 50%
  - ≥ 20% → trading halt + system review
- **Correlation control** (Rule 12):
  - < 0.30 → full allocation
  - 0.30–0.60 → reduce 20%
  - 0.60–0.70 → reduce 40%
  - ≥ 0.70 → cluster treatment

**Questions to answer before this batch:**
- Correlation rolling window length?

**Dependencies:** Batch 5

---

### BATCH 7 — Execution Engine (Layer 6, historical and superseded)

**Historical goal:** Connect to Binance Futures and execute orders. This was not
carried into the supported runtime: official execution is local paper only and
authenticated exchange access is limited to optional Demo/Testnet smoke tests.

**Questions to answer before this batch:**
- Order types to support (market, limit, conditional)?
- Any system-level leverage cap?

**Tasks:**
- Binance Futures order placement, modification, cancellation
- Execution confirmation and fill logging
- Funding payment tracking (feeds into Rule 8 trade record)
- Trade record writer (feeds into Batch 2 compliance engine)
- Early failure safeguards: excessive leverage protection, transaction-cost underestimation protection (Rule 20)

**Dependencies:** Batch 5, Batch 2

---

### BATCH 8 — Monitoring Layer (Layer 7)

**Goal:** Full observability, alerting, and kill-switch.

**Questions to answer before this batch:**
- Alert notification channel (email / Telegram / console)?

**Tasks:**
- System health tracker
- Exchange connectivity monitor
- Trade execution error detector
- Abnormal strategy behaviour detector
- Excessive drawdown detector (triggers Rule 15)
- **Kill-switch** (Rule 17): halts automatically when thresholds exceeded
- **Black swan protection** (Rule 18): volatility circuit breakers, liquidity collapse detection, exposure limits, trading suspension
- Human rule override prevention (Rule 20)

**Dependencies:** All previous batches

---

### BATCH 9 — Recursive Learning Engine

**Goal:** Post-trade analysis + agent weight updates with randomness filter.

**Questions to answer before this batch:**
- What statistical test defines the noise threshold for updates (Rule 10)?
- How are agent weights updated based on performance?

**Tasks:**
- Post-trade analysis pipeline
- Randomness filter (no updates below threshold — Rule 10)
- Agent weight update mechanism (feeds back to Decision Engine)
- Overfitting protection (Rule 20)

**Dependencies:** Batch 8

---

### BATCH 10 — Strategy Lifecycle Pipeline

**Goal:** Implement the 4-stage strategy development pipeline.

**Questions to answer before this batch:**
- What metrics define "underperforming" for return-to-research or retirement?
- Minimum sample size before evaluation?

**Tasks:**
- Strategy state machine (Research → Simulation → Pilot → Full Production — Rule 21)
- Underperforming strategy detection
- Return-to-research or retirement logic
- Strategy registry (PostgreSQL)

**Dependencies:** Batch 9

---

## OPEN QUESTIONS LOG

| # | Question | Status | Answer |
|---|----------|--------|--------|
| 1 | Programming language? | CLOSED | Python |
| 2 | Market data provider? | CLOSED | Binance |
| 3 | Broker / exchange API? | CLOSED | Binance Futures |
| 4 | Asset class? | CLOSED | USDT-Perpetual Futures |
| 5 | Infrastructure? | CLOSED | Local |
| 6 | Testing framework? | CLOSED | pytest |
| 7 | Database? | CLOSED | PostgreSQL |
| 8 | Paper trading or live? | SUPERSEDED | Local paper only; no live-money mode |
| 9 | Position sizing formula? | CLOSED | Half-Kelly + vol norm + 1–2% cap |
| 10 | Correlation thresholds? | CLOSED | 0.30 / 0.60 / 0.70 tiers |
| 11 | Z-score threshold? | CLOSED | \|Z\| > 3 |
| 12 | Half-Kelly confirmed? | CLOSED | Yes — explicit in v21.1 |
| 13 | Drawdown thresholds? | CLOSED | <5% / 5–10% / 10–15% / ≥20% |
| 14 | Portfolio risk limit? | CLOSED | 10–15% total capital |
| 15 | Regime classification? | CLOSED | 4 regimes — activate only in compatible |
| 16 | Strategy lifecycle? | CLOSED | Research → Simulation → Pilot → Full Production |
| 17 | Capital phases? | CLOSED | <$10k / $10k–$50k / $50k–$250k / $250k+ |
| 18 | Trade ID in record? | CLOSED | Yes — back in v21.1 |
| 19 | Data granularity and depth? | CLOSED | 1m primary, aggregate to 5m/15m/1h internally; 3 years min (5 years if available) |
| 20 | Which USDT-perp pairs to start with? | CLOSED | BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, ADAUSDT, DOGEUSDT, LINKUSDT, AVAXUSDT, MATICUSDT (10 pairs) |
| 21 | Correlation rolling window length? | CLOSED | 60 periods |
| 22 | Agent weight initialization? | CLOSED | Equal weights 1.0; updated via EMA of accuracy |
| 23 | Learning update statistical test? | CLOSED | Both: min 30 trades AND binomial p < 0.05 |
| 24 | Binance order types to support? | CLOSED | Market + Limit + Stop-Limit |
| 25 | System-level leverage cap? | CLOSED | 5× |
| 26 | Alert notification channel? | CLOSED | Console (structlog) + Telegram |
| 27 | Underperforming strategy metrics? | OPEN | — |

---

## HISTORICAL STATUS SNAPSHOT

```
Active Batch:    None — Batch 8 complete, ready for Batch 9
Last Shipped:    Batch 8 (2026-03-13) — Monitoring Layer
Next Action:     Approve Batch 9 (Recursive Learning Engine — post-trade analysis, agent weight updates)
```

---

*Source: MAAIS v21.1*
*Last updated: 2026-03-12*
