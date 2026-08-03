# BEHAVIOURS.md — System Constraints

> **Historical source extraction, not current operator instructions.** Use
> `AGENTS.md`, `README.md`, and `docs/runbooks/` for the implemented paper-only
> runtime. Any production or live-money interpretation is superseded.
>
> **CRITICAL:** This file contains ONLY constraints explicitly stated in the MAAIS v21.1 PDF.
> NO assumptions. NO interpretations. NO additions.
> Every rule here is a direct, verbatim-faithful extraction from the source document.
> If something is not in the PDF, it is NOT in this file.

---

## RULE 1 — Trade Execution Gate

**Source:** Part II, Section 3 — Expected Value Trade Selection

A trade is executed **only if EV > 0 after costs**.

```
EV = P(win) × Gain − P(loss) × Loss
```

EV calculations must incorporate:
- Probability estimates from agents
- Risk-reward ratio
- Volatility conditions
- Trading costs
- Funding-rate carry

> "A trade is executed only if: EV > 0 after costs."

---

## RULE 2 — Positive Alpha After All Costs

**Source:** Part I, Section 2 — Alpha and Statistical Edge

A profitable trading strategy must generate **positive alpha after costs**.

```
α = Strategy Return − Market Return
```

Costs that must be accounted for:
- Exchange fees
- Slippage
- Bid-ask spreads
- Funding costs

> "A profitable trading strategy must generate positive alpha after costs."

---

## RULE 3 — Agent Output Requirements (4 Outputs)

**Source:** Part III, Section 4 — Analytical Agent Network

Every agent **must** produce exactly **four** outputs:

1. Directional hypothesis
2. Probability estimate
3. Confidence score
4. Risk estimate

> "Each agent produces: directional hypothesis, probability estimate, confidence score, risk estimate"

---

## RULE 4 — Agent List (Exactly 8 Agents)

**Source:** Part III, Section 4 — Analytical Agent Network

The system consists of **exactly these 8 agents**:

1. Momentum Agent
2. Technical Structure Agent
3. Liquidity Agent
4. Order Flow Toxicity Agent
5. Stop-Run Detection Agent
6. Mean Reversion Agent
7. Carry Yield Agent
8. Macro Sentiment Agent

> "The system consists of multiple specialized analytical agents: Momentum Agent, Technical Structure Agent, Liquidity Agent, Order Flow Toxicity Agent, Stop-Run Detection Agent, Mean Reversion Agent, Carry Yield Agent, Macro Sentiment Agent"

---

## RULE 5 — Adversarial Decision Protocol

**Source:** Part III, Section 5 — Adversarial Decision Protocol

Agent outputs are aggregated through an **adversarial reasoning process**.

Agents challenge each other's predictions before the final decision engine evaluates:
- Probability consensus
- Confidence weighting
- Expected value

This prevents reliance on a single model.

> "Agent outputs are aggregated through an adversarial reasoning process. Agents challenge each other's predictions before the final decision engine evaluates: probability consensus, confidence weighting, expected value."

---

## RULE 6 — Z-Score Threshold for Mean Reversion

**Source:** Part V, Section 8.1 — Asymmetric Mean Reversion

```
Z = (Price − Mean) / Standard Deviation
```

**Extreme deviations where |Z| > 3 indicate possible mean reversion opportunities.**

> "Extreme deviations where |Z| > 3 indicate possible mean reversion opportunities."

---

## RULE 7 — Funding Rates in EV Calculation

**Source:** Part VI, Section 10.1 — Cost-of-Carry Opportunities

Perpetual futures markets include **funding payments** exchanged between long and short traders.

**Funding rates are incorporated into expected value calculations.**

> "Funding rates are incorporated into expected value calculations."

---

## RULE 8 — Mandatory Trade Record Fields (11 Fields)

**Source:** Part IX, Section 11.3 — Compliance & Trade Logging

Every trade record **must** include **all** of the following fields:

| Field | Requirement |
|-------|-------------|
| Trade ID | Required |
| Timestamp | Required |
| Asset | Required |
| Entry Price | Required |
| Exit Price | Required |
| Position Size | Required |
| Fees | Required |
| Funding Paid / Received | Required |
| Profit / Loss | Required |
| Exchange Rate | Required |
| Strategy ID | Required |

> "Each trade record includes: Trade ID, Timestamp, Asset, Entry Price, Exit Price, Position Size, Fees, Funding Paid / Received, Profit / Loss, Exchange Rate, Strategy ID"

---

## RULE 9 — FIFO Accounting

**Source:** Part IX, Section 11.3 — Compliance & Trade Logging

Positions are tracked using **FIFO accounting**.

> "Positions are tracked using FIFO accounting."

---

## RULE 10 — Randomness Filter for Strategy Updates

**Source:** Part IX, Section 11.1 — Randomness Filter

Strategy updates occur **only when performance deviations exceed statistical noise thresholds**.

> "Strategy updates occur only when performance deviations exceed statistical noise thresholds."

---

## RULE 11 — Portfolio Risk Limit

**Source:** Part VIII, Section 12 — Portfolio Risk Limit

Total simultaneous portfolio risk is limited to **10–15% of total capital**.

> "Total simultaneous portfolio risk is limited to: 10–15% of total capital"

---

## RULE 12 — Correlation Control Thresholds

**Source:** Part VIII, Section 12.1 — Correlation Control

| Correlation Range | Action |
|------------------|--------|
| Corr < 0.30 | Full allocation |
| 0.30 – 0.60 | Reduce allocation 20% |
| 0.60 – 0.70 | Reduce allocation 40% |
| ≥ 0.70 | Treat as correlated cluster |

> "Corr < 0.30 → full allocation / 0.30–0.60 → reduce allocation 20% / 0.60–0.70 → reduce allocation 40% / 0.70 → treat strategies as correlated cluster"

---

## RULE 13 — Regime Classification (Strategies Activate Only in Compatible Regimes)

**Source:** Part VIII, Section 13 — Market Regime Classification

The system classifies market conditions into four regimes:
- Trending
- Range-Bound
- High Volatility
- Low Volatility

**Strategies activate only in compatible regimes.**

> "The system classifies market conditions into four regimes: Trending, Range-Bound, High Volatility, Low Volatility. Strategies activate only in compatible regimes."

---

## RULE 14 — Position Sizing: Half-Kelly Three-Constraint Model

**Source:** Part VII, Section 15 — Position Sizing Framework

Position sizing uses **three constraints**:
- **Half-Kelly allocation**
- Volatility normalization
- Maximum risk cap

```
Position Size = min(Half-Kelly Size, Volatility-Adjusted Size, Risk Cap)
```

**Maximum risk per trade: 1–2% of total capital.**

> "Position sizing uses three constraints: Half-Kelly allocation, volatility normalization, maximum risk cap. Position Size = min(Half-Kelly Size, Volatility-Adjusted Size, Risk Cap). Maximum risk per trade: 1–2% of total capital."

---

## RULE 15 — Drawdown Risk Control

**Source:** Part VII, Section 15.2 — Drawdown Risk Control

| Drawdown | Action |
|----------|--------|
| < 5% | Normal risk |
| 5% – 10% | Reduce position sizes by 25% |
| 10% – 15% | Reduce position sizes by 50% |
| ≥ 20% | Trading halt and system review |

> "< 5% Normal risk / 5–10% Reduce position sizes by 25% / 10–15% Reduce position sizes by 50% / ≥20% Trading halt and system review"

---

## RULE 16 — Data Integrity Layer (6 Validation Checks)

**Source:** Section 22 — Data Integrity & Market Data Validation

Incoming market data must be validated through **all** of:
- Missing-data detection
- Price-outlier filtering
- Cross-exchange comparison
- Timestamp synchronization
- API outage detection
- Historical dataset validation

> "Incoming market data is validated through: missing-data detection, price-outlier filtering, cross-exchange comparison, timestamp synchronization, API outage detection, historical dataset validation"

---

## RULE 17 — Kill-Switch Mechanism

**Source:** Section 24 — Monitoring, Alerts & Kill-Switch System

A **kill-switch halts trading automatically** if safety thresholds are exceeded.

Monitoring must track:
- System health
- Exchange connectivity
- Abnormal strategy behavior
- Trade execution errors
- Excessive drawdowns

> "A kill-switch halts trading automatically if safety thresholds are exceeded."

---

## RULE 18 — Black Swan Protection

**Source:** Section 25 — Black Swan Protection

Extreme market conditions must be handled using:
- Volatility circuit breakers
- Liquidity collapse detection
- Maximum exposure limits
- Temporary trading suspension

> "Extreme market conditions are handled using: volatility circuit breakers, liquidity collapse detection, maximum exposure limits, temporary trading suspension"

---

## RULE 19 — Seven-Layer Infrastructure Architecture

**Source:** Section 23 — Infrastructure & System Architecture

The system must be built as **seven modular components**:

1. Market Data Layer
2. Feature Engineering Pipeline
3. Strategy Agent Engine
4. Decision Engine
5. Risk Engine
6. Execution Engine
7. Monitoring Layer

> "System components include: Market Data Layer, Feature Engineering Pipeline, Strategy Agent Engine, Decision Engine, Risk Engine, Execution Engine, Monitoring Layer"

---

## RULE 20 — Early Failure Mode Safeguards

**Source:** Section 21 — Early Failure Modes in Algorithmic Trading

The architecture must include safeguards for each of:
- Overfitting to historical data
- Underestimated transaction costs
- Excessive leverage
- Correlated strategies
- Human rule overrides

> "Common early failure causes include: overfitting to historical data, underestimated transaction costs, excessive leverage, correlated strategies, human rule overrides"

---

## RULE 21 — Strategy Development Pipeline (4 Stages)

**Source:** Part X, Section 26 — Strategy Development Pipeline

Strategies progress through **four stages**:

```
1. Research
      ↓
2. Simulation
      ↓
3. Pilot
      ↓
4. Full Production
```

**Underperforming strategies return to research or are retired.**

> "Strategies progress through four stages: Research, Simulation, Pilot, Full Production. Underperforming strategies return to research or are retired."

---

## RULE 22 — Capital Growth Phases

**Source:** Part XI, Section 27 — Capital Growth Phases

| Phase | Capital Range | Label |
|-------|--------------|-------|
| Phase 1 | < $10k | Validation |
| Phase 2 | $10k – $50k | Diversification |
| Phase 3 | $50k – $250k | Portfolio optimization |
| Phase 4 | $250k+ | Institutional scale |

> "Phase 1: < $10k — validation / Phase 2: $10k–$50k — diversification / Phase 3: $50k–$250k — portfolio optimization / Phase 4: $250k+ — institutional scale"

---

## WHAT IS STILL OPEN (not yet specified in any version)

- Rolling window length for correlation calculation
- Minimum sample size before strategy retirement is triggered
- Specific statistical test for learning update threshold
- Agent weight initialization and update mechanism
- Alert notification channel
- Binance order types to support
- System-level leverage cap
- Data granularity and historical depth
- Which USDT-perp pairs to start with

---

*Source: MAAIS v21.1*
*Last updated: 2026-03-12*
