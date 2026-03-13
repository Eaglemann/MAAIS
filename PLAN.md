# PLAN.md — Short-Term Development Plan

> Near-term execution plan.
> Full architecture in `ROADMAP.md`. Development process in `ORCHESTRATOR.md`.

---

## ALL CONFIRMED DECISIONS

| Decision | Value | Source |
|----------|-------|--------|
| Language | Python | You |
| Exchange | Binance | You |
| Data | Binance (live + historical) | You |
| Asset class | USDT-Perpetual Futures | You |
| Infrastructure | Local | You |
| Testing | pytest | You |
| Database | PostgreSQL | You |
| Mode | Live from day one | You |
| Position sizing | `min(Half-Kelly Size, Volatility-Adjusted Size, Risk Cap)` | v21.1 §15 |
| Half-Kelly | Explicitly confirmed | v21.1 §15 |
| Max risk per trade | 1–2% of total capital | v21.1 §15 |
| Portfolio risk limit | 10–15% of total capital | v21.1 §12 |
| Drawdown: < 5% | Normal risk | v21.1 §15.2 |
| Drawdown: 5–10% | Reduce position sizes 25% | v21.1 §15.2 |
| Drawdown: 10–15% | Reduce position sizes 50% | v21.1 §15.2 |
| Drawdown: ≥ 20% | Trading halt + system review | v21.1 §15.2 |
| Correlation thresholds | < 0.30 full / 0.30–0.60 −20% / 0.60–0.70 −40% / ≥ 0.70 cluster | v21.1 §12.1 |
| Z-score trigger | \|Z\| > 3 | v21.1 §8.1 |
| Regime classification | 4 regimes, activate only in compatible | v21.1 §13 |
| Strategy lifecycle | Research → Simulation → Pilot → Full Production | v21.1 §26 |
| Capital phases | < $10k / $10k–$50k / $50k–$250k / $250k+ | v21.1 §27 |
| Trade record | 11 fields including Trade ID, Funding Paid/Received, Strategy ID | v21.1 §11.3 |

---

## CURRENT POSITION

**Status:** Batch 0 complete — ready for Batch 1
**Capital Phase:** Phase 1 (< $10k — Validation)
**Active Batch:** None — Batch 0 shipped 2026-03-12

---

## BATCH 0 — Project Foundation (SHIPPED 2026-03-12)

**All items complete. 11/11 tests passing.**

- [x] Directory structure: 7 layers + `config/`, `db/`, `core/`
- [x] `pyproject.toml` + uv (SQLAlchemy, Alembic, psycopg3, DuckDB, Kafka, pydantic-settings, structlog, pytest)
- [x] `.env.template`
- [x] SQLAlchemy 2.0 async engine + Alembic baseline migration
- [x] `config/constants.py` — all thresholds from BEHAVIOURS.md
- [x] structlog setup (JSON/console depending on environment)
- [x] pytest scaffold with fixtures

---

## BATCH 1 — Market Data Layer (BLOCKED — needs answers)

**Open questions before starting:**

---

## OPEN QUESTIONS (needed as we reach each batch)

| Batch | Question |
|-------|----------|
| ~~1~~ | ~~What data granularity?~~ | CLOSED: 1m primary, aggregate to 5m/15m/1h |
| ~~1~~ | ~~Historical data depth?~~ | CLOSED: 3 years min, 5 years if available |
| ~~1~~ | ~~Which USDT-perp pairs?~~ | CLOSED: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, ADAUSDT, DOGEUSDT, LINKUSDT, AVAXUSDT, MATICUSDT |
| 6 | Correlation rolling window length? |
| 7 | Binance order types to support? |
| 7 | System-level leverage cap? |
| 8 | Alert notification channel? |
| 9 | Statistical test for learning update threshold? |
| 9 | Agent weight initialization and update logic? |
| 10 | What defines "underperforming" for strategy retirement? |

---

*Source: MAAIS v21.1*
*Last updated: 2026-03-12*
