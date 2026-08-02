# MAAIS Paper Platform Master Delivery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify the complete local MAAIS live-data paper-trading platform, Mission Control audit dashboard, recovery system, and seven-day experiment preflight defined in the design specification.

**Architecture:** A single trading worker owns deterministic market-data processing and paper-account mutations. PostgreSQL is the authoritative append-only event and projection store; DuckDB/Parquet hold reproducible market/research data. A separate FastAPI process serves a built React dashboard and queues audited control commands without directly mutating trading state.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy 2, Alembic, PostgreSQL 16, DuckDB/Parquet, FastAPI, Pydantic, React/TypeScript/Vite, Plotly, Docker Compose, pytest, Hypothesis, Ruff, Pyright, Playwright.

## Global Constraints

- Single operator and one 10,000 USDT paper account.
- One-way positions: one net long, short, or flat position per symbol.
- Internal domain types are exchange-agnostic; Binance USDT perpetuals are implemented first.
- Official P&L uses public live Binance data and the local conservative paper broker.
- Binance Demo/Testnet is protocol validation only.
- Supported modes are exactly `REPLAY`, `PAPER_LIVE`, and `TESTNET_SMOKE`; there is no `LIVE` mode.
- Production authenticated execution endpoints are absent from the paper-ready dependency graph.
- PostgreSQL is authoritative for experiments, decisions, gates, orders, fills, controls, incidents, and reports.
- DuckDB/Parquet are authoritative for frozen replay and analytical datasets.
- Every completed closed-one-minute decision cycle is persisted exactly once.
- Every configured agent receives an evaluation row, including disabled and incompatible agents.
- Rejected directional proposals receive isolated counterfactual tracking.
- Every accepted position has stop, target, maximum-hold, opposing-signal, and emergency exit behavior.
- The official first run uses 1x leverage with a hard maximum of 5x.
- Learning produces recommendations only and cannot mutate a running experiment.
- All internal timestamps are UTC; Berlin time is presentation and daily-report cutoff only.
- Official soak and seven-day candidates require a clean committed worktree and frozen manifest.
- No phase is complete until its definition of done has fresh current-state evidence.

---

## Source Documents

- Design: `docs/superpowers/specs/2026-08-02-maais-paper-trading-observability-design.md`
- Constraints: `BEHAVIOURS.md`
- Development process: `ORCHESTRATOR.md`
- Current implementation inventory: `SHIPPED.md`

## Complete Target File Map

### Runtime and domain

- `maais/domain/ids.py` - typed UUID identifiers and correlation IDs.
- `maais/domain/enums.py` - run, decision, gate, order, position, command, and incident enums.
- `maais/domain/events.py` - versioned immutable domain-event envelope and payload types.
- `maais/domain/money.py` - Decimal-based quantity, price, fee, and P&L helpers.
- `maais/domain/ports.py` - market, reference-price, clock, execution, event-store, command, and notification protocols.
- `maais/experiments/manifest.py` - immutable run configuration and hashes.
- `maais/experiments/service.py` - run lifecycle and configuration-freeze rules.
- `maais/orchestrator/service.py` - exactly-once closed-bar decision pipeline.
- `maais/orchestrator/state.py` - restored runtime cursors and warm-up state.
- `maais/orchestrator/cli.py` - setup, replay, paper, preflight, report, backup, and restore commands.

### Persistence

- `maais/db/models/` - focused SQLAlchemy models by aggregate.
- `maais/db/repositories/` - event, experiment, decision, execution, account, command, incident, and report repositories.
- `maais/db/unit_of_work.py` - transaction boundary for events, projections, and outbox.
- `maais/db/replay.py` - projection rebuild and consistency verification.
- `alembic/versions/0005_*.py` onward - forward-only platform migrations.

### Data and research

- `maais/market_data/frame_builder.py` - closed one-minute market frames.
- `maais/market_data/integrity/service.py` - mandatory validation state machine.
- `maais/market_data/reference/` - secondary-venue adapter and symbol mapping.
- `maais/market_data/backfill.py` - cursor-based REST catch-up.
- `maais/research/store.py` - DuckDB/Parquet datasets.
- `maais/research/replay.py` - deterministic replay source and clock.
- `maais/research/counterfactuals.py` - isolated rejected-proposal outcomes.
- `maais/research/learning.py` - offline weight recommendations.

### Paper execution

- `maais/execution/authorization.py` - unforgeable gate-chain authorization.
- `maais/execution/filters.py` - venue tick, step, min/max, notional, and status rules.
- `maais/execution/paper/clock.py` - live and replay clocks.
- `maais/execution/paper/orders.py` - order aggregate and state machine.
- `maais/execution/paper/fills.py` - depth, latency, queue, and partial-fill models.
- `maais/execution/paper/account.py` - cash, equity, margin, fees, funding, and reconciliation.
- `maais/execution/paper/positions.py` - one-way positions and FIFO lots.
- `maais/execution/paper/exits.py` - stops, targets, time exits, opposing signals, and flattening.
- `maais/execution/paper/broker.py` - `ExecutionPort` implementation.
- `maais/execution/testnet/` - isolated Binance Demo/Testnet smoke adapter.

### API, dashboard, and reports

- `maais/api/app.py` - FastAPI lifespan and static dashboard serving.
- `maais/api/routes/` - read, export, health, WebSocket, and control-command routes.
- `maais/api/queries/` - read projections and reconciliation queries.
- `dashboard/` - Vite React/TypeScript application.
- `dashboard/src/pages/MissionControl.tsx` - default operations overview.
- `dashboard/src/pages/AuditLedger.tsx` - complete decision/event drill-down.
- `dashboard/src/pages/ResearchLab.tsx` - performance, calibration, gate, and cost analysis.
- `dashboard/src/pages/Operations.tsx` - incidents and audited controls.
- `maais/reporting/daily.py` - immutable Berlin-day report snapshots.
- `maais/reporting/final.py` - seven-day aggregation.
- `maais/reporting/exports.py` - Markdown, JSON, CSV, and Parquet artifacts.

### Operations and verification

- `compose.yaml`, `Dockerfile`, `dashboard/Dockerfile` - reproducible local platform.
- `.github/workflows/ci.yml` - backend, PostgreSQL, frontend, safety, and browser gates.
- `scripts/` - start, stop, preflight, backup, restore, and safe paper reset wrappers.
- `docs/runbooks/` - setup, operations, recovery, incident, and seven-day experiment guides.
- `tests/unit/`, `tests/property/`, `tests/contracts/`, `tests/integration/`, `tests/e2e/`, `tests/faults/` - layered evidence.

## Plan Set and Execution Order

| Plan | Outcome | Must finish before |
|---|---|---|
| Phase 0 - Baseline safety | Clean repository, explicit modes, CI, PostgreSQL Compose, no production execution path | All platform work |
| Phase 1 - Event ledger | Immutable experiment/event/projection/outbox and decision lineage | Broker and dashboard |
| Phase 2 - Paper broker | Conservative fills, account, positions, exits, counterfactual isolation | Live orchestrator |
| Phase 3 - Orchestrator | Complete validated live/replay pipeline and recovery cursors | Dashboard preflight |
| Phase 4 - Mission Control | Query/control API, dashboard, reports, exports | Operator test |
| Phase 5 - Resilience | Fault injection, backup/restore, metrics, runbooks | Soak |
| Phase 6 - Preflight | Deterministic replay, Testnet smoke, 24-hour live-data soak | Seven-day run |
| Phase 7 - Experiment | Seven continuous days and final reconciled report | Goal completion |

## Phase 0 - Baseline Safety and Repository Health

Detailed plan: `docs/superpowers/plans/2026-08-02-maais-phase-0-baseline-safety.md`

**Deliverable:** A clean, reproducible base where paper/testnet modes are explicit, production authenticated execution cannot be constructed, PostgreSQL starts locally, static/CI gates pass, and current safety defects are captured by failing tests before later replacement.

**Definition of done:**

- Ruff format/check, Pyright, pytest, dependency audit, secret scan, and migration smoke checks pass.
- `docker compose up -d postgres` becomes healthy and Alembic upgrades from empty to head.
- `RunMode` contains only replay, paper-live, and testnet-smoke.
- Production execution URL is absent from authenticated execution modules and factories.
- Existing execution rejects unapproved decision/risk inputs and never swallows post-fill recording failure.
- README and status documents describe current reality and commands.

## Phase 1 - Experiment Event Ledger

The phase brief below fixes scope and acceptance criteria. Its implementation-grade task plan is a phase-gate artifact: it is derived from the verified Phase 0 schema/tooling interfaces immediately before Phase 1 execution, then self-reviewed against the design before code changes begin.

### Task groups

- [ ] Define typed identifiers, enums, immutable event envelope, and reason-code catalog.
- [ ] Implement experiment manifest normalization, SHA-256 hashing, clean-candidate rule, and run lifecycle.
- [ ] Add focused models and migrations for experiments, strategy/agent versions, domain events, outbox, commands, and incidents.
- [ ] Add event-store optimistic concurrency and atomic event/projection/outbox unit of work.
- [ ] Add market frame, decision cycle, agent evaluation, decision summary, gate evaluation, and proposal projections.
- [ ] Add decision-bundle read repository and complete JSON export.
- [ ] Add projection rebuild and event/projection consistency verifier.
- [ ] Prove concurrent append, rollback, exactly-once decision key, and restart reconstruction against PostgreSQL.

### Definition of done

- Every configured agent, gate, and decision field from the design has typed persistence.
- Event streams are gapless and version-conflict safe.
- Projection rebuild reproduces normalized authoritative state.
- One PostgreSQL transaction appends events, updates projections, and inserts outbox rows.

## Phase 2 - Paper Broker, Account, and Exits

### Task groups

- [x] Add deterministic live/replay clock and order eligibility timestamps.
- [x] Add exchange-filter snapshots, conservative Decimal quantization, and risk-preserving rejection.
- [x] Add unforgeable execution authorization linked to the complete gate chain.
- [x] Implement market depth walking, latency attribution, stale-book rejection, and insufficient-depth rejection.
- [x] Implement limit queue model, trade-through requirement, 10% participation cap, partial fills, cancellation, and expiry.
- [x] Implement order aggregate event transitions and idempotency.
- [x] Implement 10,000 USDT account, 1x initial leverage, margin, fees, funding, equity, and reconciliation.
- [x] Implement one-way positions and persisted FIFO lots.
- [x] Implement one-ATR stop, one-ATR target, 60-bar time exit, two-bar opposing-signal exit, and emergency flatten.
- [x] Implement isolated counterfactual broker and 15m/1h/4h/24h outcomes.
- [x] Add optimistic/conservative/stress execution sensitivity records.
- [x] Prove accounting and isolation invariants with Hypothesis and PostgreSQL integration tests.

### Definition of done

- Frozen input events reproduce the same order, fill, account, exit, and counterfactual events.
- No fill uses future market information.
- Official account and counterfactual state have mechanically separate repositories.
- Cash, margin, positions, fees, funding, and P&L reconcile after partial opens and closes.

## Phase 3 - Validated End-to-End Orchestrator

**Status:** In progress. Detailed execution plan: `docs/superpowers/plans/2026-08-02-maais-phase-3-validated-orchestrator.md`.

### Task groups

- [x] Build exactly-once one-minute frames from live and replay events.
- [x] Expand data integrity into pass/fail/not-applicable quarantine and recovery states.
- [ ] Add true secondary-venue reference prices and separate futures/spot basis validation.
- [ ] Add venue clock drift, sequence, duplicate, crossed-book, OHLC, volume, symbol-state, and stale checks.
- [ ] Add REST gap backfill with deterministic downstream recomputation.
- [ ] Persist all eight agent rows, reason codes, durations, maturity, and proxy labeling.
- [ ] Correct price, correlation warm-up, risk-at-stop, persistent drawdown, and rolling black-swan baselines.
- [ ] Wire decision, monitoring, risk, authorization, paper broker, exits, counterfactuals, and outbox in sequence.
- [ ] Add explicit benchmark alpha and prohibit zero benchmark defaults in official runs.
- [ ] Add worker lifecycle, graceful shutdown, resume, cursor reconciliation, and CLI.
- [ ] Prove full accepted, neutral, rejected, quarantined, partial-fill, exit, halt, and recovery traces.

### Definition of done

- A replay fixture drives the full pipeline and produces a golden normalized event stream.
- Live-data mode can run without any API key.
- Failed required data checks never feed a tradable proposal.
- Gate order and execution authorization cannot be bypassed.

## Phase 4 - Mission Control and Reporting

### Task groups

- [ ] Add read-only query models and FastAPI lifespan.
- [ ] Add experiment, overview, position, order, decision, agent, gate, counterfactual, incident, and report endpoints.
- [ ] Add outbox-cursor WebSocket updates and reconnect catch-up.
- [ ] Add command inbox endpoints with bearer token, confirmation, idempotency, and worker results.
- [ ] Build React shell and Mission Control page from the approved visual direction.
- [ ] Build Audit Ledger filters, event timeline, decision lineage, and exports.
- [ ] Build Research Lab equity/drawdown, cost waterfall, distribution, calibration, attribution, benchmark, gate, and sensitivity views.
- [ ] Build Operations incident, health, command, kill-switch, and flatten views.
- [ ] Add immutable Berlin-day Markdown/JSON/CSV/Parquet reports and final aggregation.
- [ ] Prove dashboard values and report hashes reconcile to PostgreSQL.

### Definition of done

- Every displayed number links to an authoritative query and run identity.
- Every decision can be explained from market frame through outcome.
- Dashboard restart does not affect the worker and resumes from its cursor.
- Controls never update trading projections directly.

## Phase 5 - Resilience and Operational Hardening

### Task groups

- [ ] Persist and restore kill switch, account, positions, exit plans, orders, cursors, drawdown, and correlation windows.
- [ ] Add structured metrics, JSON logs, lag, queue, state, incident, and report freshness signals.
- [ ] Add Telegram alert routing and alert deduplication.
- [ ] Add daily PostgreSQL and artifact backup plus verified restore script.
- [ ] Add WebSocket, missing/duplicate/reordered data, stale book, venue error, database outage, disk-full, worker kill, API kill, and clock-drift fault tests.
- [ ] Add setup, operations, incident, recovery, preflight, and seven-day runbooks.
- [ ] Prove no duplicate decisions, orders, fills, commands, or reports after each recovery scenario.

### Definition of done

- Restore drill reconstructs a known fixture experiment and matching reconciliation hashes.
- Every injected fault creates the specified halt/incident and recovers only through its gate.
- Required alerts are visible in Mission Control and configured notification channels.

## Phase 6 - Preflight Candidate

### Task groups

- [ ] Freeze official code commit, configuration, fee schedule, symbol filters, weights, strategy, and fill policy.
- [ ] Run complete historical deterministic replay and archive artifacts.
- [ ] Run Binance Demo/Testnet create/query/cancel/reduce-only protocol smoke tests with isolated credentials.
- [ ] Run backup and restore drill on the candidate.
- [ ] Run 24 continuous hours of public live data with paper broker and all reports.
- [ ] Audit decision cardinality, data gaps, recovery, event/projection consistency, account reconciliation, counterfactual isolation, dashboard/report equality, errors, and incidents.
- [ ] Produce signed preflight evidence manifest and readiness verdict.

### Definition of done

- All 15 design readiness requirements have current direct evidence.
- There are no unexplained errors, state differences, gaps, duplicates, or report discrepancies.
- Only then announce that the system is ready to start the seven-day test.

## Phase 7 - Seven-Day Paper Experiment

### Task groups

- [ ] Start the clean frozen candidate with 10,000 USDT and one-way positions.
- [ ] Monitor health and incidents without mutating strategy/configuration.
- [ ] Generate and reconcile each Berlin-day report.
- [ ] Preserve every decision, counterfactual, order, fill, exit, control, restart, and incident.
- [ ] Complete day-seven open-state cutoff and final report aggregation.
- [ ] Audit all requirements and publish engineering, behavior, cost, risk, calibration, gate-value, and limitation findings.

### Definition of done

- Seven continuous days have complete authoritative records and daily/final artifacts.
- No unexplained state remains.
- The final report distinguishes engineering validity from statistical strategy evidence.

## Commit and Review Policy

- Each detailed task starts with a failing test, implements the smallest correct behavior, and ends with focused plus relevant regression tests.
- Review the diff and current evidence before each task is marked complete.
- Commit only after explicit user authorization; do not infer commit or push permission from this plan.
- Do not push, deploy, enable credentials, or start the official timed experiment without explicit user authorization.
