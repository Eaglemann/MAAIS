# MAAIS Paper-Trading and Observability Design

**Status:** Design baseline for implementation
**Date:** 2026-08-02
**Owner and operator:** Single user
**Initial experiment:** Seven continuous days, 10,000 USDT virtual capital, Binance USDT perpetual market data, no real-money execution

## 1. Purpose

MAAIS will become a professional single-operator quantitative-trading workstation that supports the complete strategy lifecycle:

`research -> deterministic replay -> walk-forward evaluation -> live-data paper trading -> optional controlled testnet smoke tests -> future pilot -> production`

This design covers the work required to make the repository ready for a trustworthy seven-day live-data paper-trading experiment with complete decision visibility. It does not claim that a successful week proves profitability or production readiness.

## 2. Fixed Decisions

- The system is for one operator and one paper account.
- The official seven-day account starts with 10,000 USDT.
- The official experiment uses one-way positions: one net long, short, or flat position per symbol.
- The primary experiment uses public live Binance market data and a local paper broker.
- Binance Demo/Testnet is a separate protocol smoke-test adapter, not the P&L source of truth.
- Internal domain types are exchange-agnostic; Binance is the first implemented venue.
- Every completed one-minute decision cycle is persisted, including neutral and rejected outcomes.
- Rejected directional proposals receive counterfactual tracking without affecting account state or risk.
- Every accepted position has an automatic exit plan.
- Agent-weight learning produces offline recommendations only. It cannot mutate a running experiment.
- The Mac is expected to remain powered, awake, and connected, but crash recovery and gap backfill remain mandatory.
- Mission Control is the default dashboard; Audit Ledger and Research Lab are drill-down views.
- The dashboard includes audited operator controls.
- PostgreSQL is the authoritative operational and audit store.
- DuckDB and Parquet are the analytical and reproducible market-data stores.
- Kafka is not on the critical path for the first paper platform. Stable domain events and an outbox preserve a later migration path.
- Real-money execution is not part of this design and cannot be enabled through configuration.

## 3. Definition of Ready for the Seven-Day Test

The system is ready only when all of the following evidence exists:

1. A documented command starts the local platform from a clean checkout.
2. The paper mode cannot instantiate or call a production authenticated execution client.
3. Public market data flows through integrity checks, features, agents, decision, monitoring, risk, paper execution, accounting, and reporting.
4. Decision approval, monitoring approval, risk approval, and calculated quantity are enforced by the execution boundary.
5. Every completed decision cycle is queryable with its exact inputs, agent outputs, gate results, proposal disposition, and later outcome.
6. The paper account, positions, orders, exit plans, funding, fees, P&L, drawdown, and controls survive a process restart.
7. Counterfactual results remain completely isolated from official paper-account state.
8. A deterministic replay produces byte-stable normalized domain results from a frozen fixture dataset.
9. Database migrations and ORM models agree on a real PostgreSQL instance.
10. Unit, property, contract, integration, end-to-end, recovery, and fault-injection tests pass.
11. Lint and type checks pass with no ignored high-severity findings.
12. Docker Compose health checks, backup, restore, and restart drills pass.
13. A frozen run manifest records code, configuration, weights, fees, fill policy, symbols, and data versions.
14. A 24-hour live-data soak completes with no unexplained state, duplicate decisions, unhandled exceptions, or unrecovered gaps.
15. Mission Control and the generated daily report reconcile exactly to authoritative PostgreSQL records.

Green unit tests, a rendering dashboard, Testnet profit, or one profitable day are not sufficient evidence.

## 4. Runtime Architecture

### 4.1 Local processes

The initial deployment contains two application processes and two storage services:

1. **MAAIS worker**
   - Owns market-data subscriptions, gap backfill, feature calculation, agent execution, decision evaluation, monitoring, risk, paper execution, account state, exits, counterfactuals, and event persistence.
   - Has write access to the trading schema and analytical store.
   - Exposes no direct browser endpoints.

2. **MAAIS API and dashboard**
   - FastAPI serves read-only query endpoints, WebSocket updates, audited control commands, and the built React application.
   - Reads PostgreSQL projections through a read-oriented repository.
   - Cannot directly mutate account, order, or position tables.
   - Writes commands to an append-only command inbox; the worker validates and applies them.

3. **PostgreSQL**
   - Authoritative event, decision, execution, account, control, incident, and reporting store.

4. **DuckDB and Parquet**
   - One-minute market history, normalized replay inputs, derived research datasets, and exported experiment artifacts.

Docker Compose manages PostgreSQL, worker, API/dashboard, health checks, persistent volumes, and local networking. The React build is served by FastAPI in the packaged local deployment.

### 4.2 Ports and adapters

Domain services depend on interfaces rather than Binance or wall-clock implementations:

- `MarketDataPort`: live, recorded replay, and fixture sources.
- `ReferencePricePort`: true secondary-venue reference prices used by cross-venue validation.
- `ExecutionPort`: paper broker and Binance Demo/Testnet adapter.
- `ClockPort`: live UTC clock and deterministic replay clock.
- `EventStorePort`: atomic event append and optimistic stream versioning.
- `MarketStorePort`: DuckDB/Parquet persistence and replay queries.
- `AccountRepositoryPort`: authoritative paper-account projections.
- `NotificationPort`: structured log and Telegram implementations.
- `ControlCommandPort`: audited operator commands.

There is intentionally no production execution adapter in the paper-ready build.

### 4.3 Domain event boundary

All state changes are represented by versioned domain events. PostgreSQL appends the event and updates its current-state projection in one transaction. An outbox row in that transaction drives dashboard updates and preserves a future Kafka migration path.

At minimum, event families include:

- Experiment started, paused, resumed, stopped, completed, or failed.
- Market frame accepted, quarantined, backfilled, or superseded.
- Decision cycle completed.
- Agent evaluation recorded.
- Proposal approved, rejected, neutral, or expired.
- Gate passed or failed.
- Order intent created, validated, accepted, partially filled, filled, canceled, rejected, or expired.
- Exit plan created, adjusted, triggered, or completed.
- Position opened, increased, reduced, reversed, or closed.
- Fee, funding, realized P&L, and unrealized P&L recorded.
- Account snapshot and drawdown updated.
- Counterfactual opened, evaluated, and closed.
- Kill switch triggered or reset.
- Operator command requested, accepted, rejected, or completed.
- Incident opened, acknowledged, resolved, or escalated.
- Weight recommendation generated, reviewed, accepted for a future run, or rejected.

## 5. Experiment and Versioning Model

Every run has an immutable manifest containing:

- Run ID, name, mode, creation time, start time, end time, and status.
- Initial capital and account currency.
- Git commit SHA plus a content hash when a development run uses a dirty worktree.
- Python package lock hash and database schema revision.
- Complete normalized configuration JSON and SHA-256 hash.
- Symbol universe and exchange metadata snapshot.
- Feature, agent, decision, risk, exit, and fill-policy versions.
- Agent weights and maturity status.
- Fee schedule and funding policy.
- Clock and latency policy.
- Market-data source and secondary reference source.

Configuration cannot change inside a running experiment. Any change closes or pauses the current run and creates a new manifest and run ID.

Development runs may record a dirty-worktree content hash. The official 24-hour soak and seven-day candidate require a clean committed worktree so the exact code can be reproduced.

Supported run modes are:

- `REPLAY`: deterministic recorded data and replay clock.
- `PAPER_LIVE`: public live data and local paper broker.
- `TESTNET_SMOKE`: Binance Demo/Testnet protocol validation with isolated credentials and records.

No `LIVE` mode exists in the initial schema, CLI, configuration enum, or dependency graph.

## 6. Authoritative Data Model

All operational tables use UUID primary keys, UTC `timestamptz`, explicit schema versions, and foreign keys. JSONB captures versioned snapshots; frequently filtered and aggregated values use typed columns and indexes.

### 6.1 Experiment tables

#### `experiments`

- `id`, `name`, `mode`, `status`
- `initial_capital`, `currency`
- `started_at`, `ended_at`, `created_at`
- `git_sha`, `worktree_hash`, `lock_hash`, `schema_revision`
- `config_json`, `config_hash`
- `failure_reason`

#### `strategy_versions`

- `id`, `strategy_key`, `version`, `stage`
- `implementation_hash`, `parameter_json`
- `created_at`, `retired_at`

Stages are research, simulation, pilot, and full production. This design uses research and simulation only.

#### `agent_versions`

- `id`, `agent_name`, `version`, `maturity`
- `implementation_hash`, `parameter_json`, `data_dependencies_json`
- `enabled`, `created_at`

Maturity is `IMPLEMENTED`, `PROXY`, or `DISABLED`. Proxy agents are visibly labeled and cannot be mistaken for their intended future implementation.

### 6.2 Market and decision tables

#### `market_frames`

- `id`, `experiment_id`, `symbol`, `venue`, `timeframe`
- `bar_open_at`, `bar_close_at`, `observed_at`
- `open`, `high`, `low`, `close`, `volume`
- `best_bid`, `best_ask`, `mark_price`, `funding_rate`
- `orderbook_snapshot_json`, `source_sequence_json`
- `quality_status`, `quality_results_json`, `content_hash`

#### `decision_cycles`

- `id`, `experiment_id`, `market_frame_id`, `strategy_version_id`
- `symbol`, `timeframe`, `cycle_at`, `regime`
- `feature_snapshot_json`, `feature_version`
- `status`, `direction`, `disposition`, `reason_code`
- `created_at`, `completed_at`

There is a unique constraint on `(experiment_id, symbol, timeframe, cycle_at, strategy_version_id)` to guarantee exactly-once decision cycles.

#### `agent_evaluations`

- `id`, `decision_cycle_id`, `agent_version_id`
- `compatible`, `enabled`, `weight`
- `direction`, `probability`, `confidence`, `risk`
- `input_snapshot_json`, `reason_codes_json`, `explanation_json`
- `duration_ms`, `created_at`

Exactly one row is recorded for every configured agent. Incompatible or disabled agents retain a row explaining why they did not vote.

#### `decision_summaries`

- `decision_cycle_id`
- Consensus direction, probability, confidence, and vote weights.
- Dissenters, dissent probability, dissent confidence, and challenge outcome.
- Expected gain, expected loss, gross EV, funding carry, estimated costs, and net EV.
- Benchmark return and alpha estimate.
- Full versioned consensus, adversarial, EV, and cost snapshots.

#### `gate_evaluations`

- `id`, `decision_cycle_id`, `gate_type`, `sequence`
- `passed`, `reason_code`, `input_json`, `output_json`
- `evaluated_at`, `duration_ms`

Gate types include data quality, regime compatibility, consensus, adversarial, EV, alpha, monitoring, drawdown, correlation, portfolio risk, leverage, exchange filters, and paper-broker capacity.

### 6.3 Proposal, execution, and account tables

#### `trade_proposals`

- `id`, `decision_cycle_id`, `experiment_id`, `symbol`, `direction`
- `status`, `reason_code`, `proposed_at`, `expires_at`
- `entry_policy_json`, `exit_policy_json`, `sizing_snapshot_json`
- `approved_quantity`, `approved_notional`, `risk_at_stop`

#### `order_intents`

- `id`, `proposal_id`, `experiment_id`, `client_order_id`
- `symbol`, `side`, `position_effect`, `order_type`
- `quantity`, `limit_price`, `stop_price`, `reduce_only`
- `time_in_force`, `created_at`, `expires_at`, `status`
- `exchange_filter_snapshot_json`

#### `order_events`

- `id`, `order_intent_id`, `sequence`, `event_type`
- `event_at`, `market_frame_id`, `payload_json`
- Unique `(order_intent_id, sequence)`.

#### `fills`

- `id`, `order_intent_id`, `fill_at`, `quantity`, `price`
- `liquidity_role`, `fee`, `fee_asset`
- `spread_cost`, `depth_slippage`, `latency_slippage`, `total_slippage`
- `market_snapshot_json`

#### `positions`

- `id`, `experiment_id`, `symbol`, `side`, `status`
- `quantity`, `average_entry`, `mark_price`
- `initial_margin`, `maintenance_margin`, `leverage`
- `unrealized_pnl`, `realized_pnl`, `fees`, `funding`
- `opened_at`, `closed_at`, `version`

There is at most one open position per experiment and symbol.

#### `position_lots`

- Extends the existing FIFO model with `position_id`, opening fill, remaining quantity, allocated fees, funding, and close linkage.

#### `exit_plans`

- `id`, `position_id`, `version`, `status`
- `stop_price`, `target_price`, `maximum_holding_until`
- `opposing_signal_policy_json`, `created_at`, `superseded_at`

#### `account_snapshots`

- `id`, `experiment_id`, `snapshot_at`
- `cash_balance`, `equity`, `used_margin`, `free_margin`
- `gross_notional`, `risk_at_stop`, `unrealized_pnl`, `realized_pnl`
- `fees`, `funding`, `peak_equity`, `drawdown`

### 6.4 Counterfactual and learning tables

#### `counterfactuals`

- `id`, `proposal_id`, `decision_cycle_id`, `status`
- `hypothetical_entry_policy_json`, `hypothetical_fill_json`
- `maximum_favorable_excursion`, `maximum_adverse_excursion`
- `outcome_15m`, `outcome_1h`, `outcome_4h`, `outcome_24h`
- `hypothetical_exit_reason`, `hypothetical_pnl`, `closed_at`

Counterfactual services have no reference to account mutation repositories.

#### `weight_recommendations`

- `id`, `experiment_id`, `agent_version_id`
- Sample size, wins, losses, calibration metrics, test statistic, p-value.
- Current weight, recommended weight, rationale, status, created_at.

Recommendations require at least 30 eligible trades and binomial `p < 0.05`. Acceptance only changes the manifest of a future run.

### 6.5 Operations and reporting tables

- `domain_events`: append-only stream, aggregate ID/type, stream version, event type/version, payload, metadata, timestamp.
- `outbox_events`: unpublished/published dashboard-event cursor.
- `control_commands`: requester, command type, confirmation, payload, status, timestamps, result.
- `incidents`: severity, component, reason code, evidence, status, acknowledgement, resolution.
- `health_samples`: component, status, heartbeat, lag, queue depth, error counts, resource metrics.
- `daily_reports`: experiment, report date, status, metric snapshot, artifact paths, content hash.

## 7. Runtime Decision Flow

For each configured symbol, the worker evaluates exactly once after a complete one-minute candle closes:

1. Receive and normalize market events.
2. Close the one-minute bar and persist the replayable market frame.
3. Run all mandatory integrity checks.
4. Quarantine failed data and create a rejected decision cycle; do not calculate a tradable proposal from failed data.
5. Compute features using only observations at or before the bar close.
6. Classify regime and record compatibility for all eight agents.
7. Run enabled compatible agents and record every output and timing.
8. Calculate consensus, adversarial dissent, estimated costs, expected value, and benchmark alpha.
9. Run ordered decision gates and persist each result.
10. If directional but rejected, create a counterfactual. If neutral, record the cycle without a synthetic entry.
11. If decision-approved, run monitoring, drawdown, correlation, portfolio-risk, leverage, and exchange-filter gates.
12. Convert risk-approved notional to a valid base-asset quantity using current mark price and the recorded exchange filters.
13. Create an order intent and exit plan atomically.
14. Submit the intent through `ExecutionPort`.
15. Apply paper-broker order events and fills to FIFO lots, position, account, fees, funding, and drawdown.
16. Evaluate exit plans on each eligible market update.
17. Publish committed outbox events to Mission Control.

No execution implementation accepts a raw quantity from an untrusted caller. It accepts an approved order intent linked to the complete gate chain.

## 8. Market Data Integrity

The current validator is expanded from logging helpers into a mandatory state machine:

- Missing interval detection.
- Close-return outlier detection with explicit quarantine behavior.
- True cross-venue price comparison through `ReferencePricePort`.
- Futures-versus-spot basis validation as a separate check, not mislabeled cross-exchange comparison.
- Event sequence and timestamp synchronization.
- Local clock drift check against venue server time.
- API outage and stale-book detection.
- Historical coverage and duplicate validation.
- OHLC invariants, non-negative volume, crossed-book detection, and symbol-state validation.

Each result is `PASSED`, `FAILED`, or `NOT_APPLICABLE` with a reason. Required checks cannot silently skip. A symbol without the configured secondary reference mapping is not admitted to the official experiment.

The worker may backfill missing closed bars through REST. It must recompute downstream frames deterministically and cannot resume new entries until the symbol catches up and passes validation.

## 9. Paper Broker Model

### 9.1 Clock and look-ahead prevention

- Features use only fully closed candles.
- Each decision records completion latency.
- An order becomes eligible only after the configured execution latency and the next observed market event.
- Fills never use a candle high, low, or close that was unavailable at eligibility time.

### 9.2 Exchange filters

Price tick, quantity step, minimum quantity, maximum quantity, minimum notional, order types, and symbol status are snapshotted before the experiment. Every intent is quantized conservatively and then validated. Quantization that would exceed the risk allowance causes rejection rather than rounding up.

### 9.3 Market orders

- Buy orders start at the recorded ask; sells start at the bid.
- The broker walks recorded visible depth and calculates volume-weighted fill price.
- Fill quantity cannot exceed recorded eligible depth.
- Insufficient or stale depth rejects the order.
- Latency slippage is the difference between the decision-time executable price and first eligible executable price.

### 9.4 Limit orders

- Merely touching the limit does not fill the order.
- A fill requires eligible aggressive trade volume through the limit price after order eligibility.
- The conservative queue model assumes displayed quantity ahead equal to the displayed size at the level when the order becomes eligible.
- After that queue is consumed, the paper order receives at most 10% of subsequent qualifying aggressive volume per event.
- Partial fills, cancellation, and expiry are first-class states.

### 9.5 Stops and exit orders

- Stop triggers use the run-manifest trigger source and default to mark price.
- Triggered stop-market orders use the next eligible book and can gap beyond the stop.
- Reduce-only exit quantity cannot exceed the open position.
- Exit failures trigger an incident and persistent experiment halt.

### 9.6 Costs and funding

- Maker and taker fee rates are required manifest values obtained during preflight and cannot change inside a run.
- Funding uses the observed rate at the venue funding timestamp and the position notional/side at that time.
- Official P&L includes fees, funding, spread, depth slippage, and latency slippage.
- The official account uses the conservative fill model.
- Optimistic and stress fill models produce isolated sensitivity outcomes only.

### 9.7 Margin and leverage

- The official first run uses 1x leverage with a hard system maximum of 5x.
- Account state tracks initial margin, maintenance margin, free margin, and liquidation estimate.
- Portfolio risk is measured as loss at recorded stop, not gross notional alone.
- Gross-notional and margin caps are additional independent gates.

## 10. Entry and Exit Policy

The first official strategy version uses explicit, frozen exit rules:

- Initial stop distance is the decision engine's expected-loss percentage, which is currently one ATR, applied from actual average fill price.
- Initial target distance is the expected-gain percentage, currently one ATR, applied from actual average fill price.
- Maximum holding time is 60 closed one-minute bars after the first fill.
- A fully approved opposite-direction decision on two consecutive closed bars triggers a reduce-only market exit.
- Drawdown halt, black-swan halt, or operator emergency flatten triggers reduce-only market exits for all positions.
- Partial entries resize stop and target quantities to the actual filled position.
- Stops never move away from risk. Future trailing logic requires a new strategy version.

These defaults make the current one-ATR EV assumptions executable and reproducible. Alternative exit policies are separate strategy versions and experiments.

## 11. Risk and Monitoring Corrections

Before paper readiness, existing controls change as follows:

- Risk sizing uses current executable mark/mid price, not `zscore_mean` as a price proxy.
- A zero or negative Kelly result rejects the proposal.
- Correlation requires 60 aligned returns. Insufficient correlation history blocks new multi-symbol exposure rather than granting full allocation.
- Portfolio risk sums loss-at-stop across positions and proposed exposure.
- Drawdown state, peak equity, positions, returns, and correlation windows persist and restore.
- The 15%-20% drawdown interval remains at the 50% sizing multiplier and is explicitly recorded in configuration.
- Black-swan volatility baselines are rolling, symbol/timeframe-specific, and warm before trading.
- Component health is a required monitoring gate. Missing or stale mandatory components block new entries.
- Kill-switch state is persistent. Reset requires an audited control command and confirmation; no property exposes a reset secret.
- Compliance persistence failures halt the experiment. They are never swallowed after a fill.

## 12. Agent and Decision Integrity

- All eight configured agent rows are visible in every cycle.
- Agent outputs include structured reason codes and input contributions, not opaque prose alone.
- Probabilities and confidence are validated and later evaluated for calibration.
- The current macro-sentiment agent is labeled `PROXY` until an actual versioned macro/news data pipeline exists.
- Proxy maturity is visible in Mission Control and daily reports.
- An LLM may generate a post-decision critique or explanation from already persisted data, but it is non-authoritative and cannot change a gate, order, or running weight.
- The deterministic adversarial gate remains the trading authority until any alternative is separately replayed and calibrated.
- Benchmark alpha uses an explicit symbol/time-horizon benchmark; a default zero market return is not accepted for official reporting.

## 13. Counterfactual Tracking

Every rejected directional proposal is evaluated with the same eligible-time, fill, fee, funding, and exit assumptions as the official account, but through an isolated counterfactual repository.

Counterfactuals record:

- Which gate rejected the proposal and the full prior gate chain.
- Whether a plausible entry existed.
- Hypothetical fill and cost components.
- Maximum favorable and adverse excursion.
- Outcomes after 15 minutes, 1 hour, 4 hours, and 24 hours.
- Outcome under the proposal's standard exit plan.

Mission Control shows gate value-add: avoided loss, missed gain, no-fill, and unresolved. Counterfactual P&L is never added to equity, drawdown, exposure, agent learning samples, or official trade statistics.

## 14. Mission Control

### 14.1 Home page

- Experiment state and immutable manifest identity.
- Paper/live-data safety banner.
- Equity, realized/unrealized P&L, drawdown, exposure, risk at stop, fees, funding, and slippage.
- Open positions, exit levels, pending orders, and current regime.
- Component health, data lag, incidents, and kill-switch state.
- Live decision feed showing neutral, rejected, counterfactual, approved, and filled outcomes.

### 14.2 Audit Ledger

- Filter by run, time, symbol, direction, disposition, gate, agent, regime, strategy, order status, and outcome.
- Expand one decision into market frame, features, agent outputs, consensus, dissent, EV, costs, every gate, size, order events, fills, exit, and outcome.
- Show a chronological event timeline and exact reason codes.
- Export filtered data to CSV and complete decision bundles to JSON.

### 14.3 Research Lab

- Equity and drawdown curves.
- Gross-to-net cost waterfall.
- Expectancy, profit factor, win/loss distribution, R multiples, MFE, and MAE.
- Results by symbol, regime, strategy, agent coalition, hour, direction, and exit reason.
- Probability calibration and Brier score by agent and consensus.
- Gate counterfactual value-add and cost-sensitivity bands.
- Comparison with explicit buy-and-hold and flat-cash benchmarks.

### 14.4 Operations and controls

- Start, pause, resume, stop, emergency halt, flatten simulated positions, acknowledge incident, and request kill-switch reset.
- Safety-critical commands require confirmation and idempotency keys.
- Commands are queued and executed by the worker; the browser never changes trading tables directly.
- Dashboard failure does not stop the worker.
- API binds to localhost by default and requires a generated local bearer token for control endpoints.

## 15. Daily and Final Reports

At the end of each Berlin calendar day, the worker freezes a report snapshot and generates Markdown, JSON, CSV, and Parquet artifacts containing:

- Starting and ending equity.
- Realized and unrealized P&L.
- Fees, funding, spread, depth slippage, and latency slippage.
- Decision cycles, directional proposals, neutral outcomes, approvals, rejections, fills, partial fills, cancellations, and exits.
- Win rate, average win/loss, expectancy, profit factor, R distribution, MFE, and MAE.
- Maximum drawdown, peak exposure, risk at stop, and margin usage.
- Results by symbol, regime, strategy, direction, agent coalition, and exit reason.
- Counterfactual gate value-add.
- Data gaps, reconnects, quarantines, incidents, operator commands, and process restarts.
- Open positions, pending orders, and unresolved counterfactuals at report cutoff.
- Reconciliation hashes linking report metrics to authoritative records.

The final seven-day report aggregates daily snapshots without recomputing or rewriting them and includes configuration, code, and dataset identities.

## 16. Failure and Recovery Rules

### 16.1 Market-data failure

- Stale, missing, out-of-sequence, crossed, or unvalidated data pauses new entries for the affected symbol.
- Existing paper positions retain their last state while an incident is visible.
- REST backfill catches up closed bars.
- The symbol resumes only after integrity checks pass and decision-cycle uniqueness is verified.

### 16.2 PostgreSQL failure

- The worker performs no state transition that cannot be persisted atomically.
- Loss of database availability halts new decisions and order processing before mutation.
- Recovery requires event/projection consistency verification.

### 16.3 Worker restart

- Rebuild current state from projections and validate it against event stream versions.
- Restore kill switch, account, positions, orders, exit plans, drawdown, correlation windows, and event cursors.
- Backfill market data from the last committed cursor.
- Reconcile exactly-once decision keys before resuming.

### 16.4 API/dashboard failure

- Worker continues independently.
- On reconnect, the API resumes outbox delivery from the last acknowledged cursor.

### 16.5 Clock or configuration drift

- UTC is authoritative internally; Berlin time is display/reporting only.
- Excessive local/venue clock drift blocks the experiment.
- Configuration-hash changes require a new run.

## 17. Testing Strategy

### 17.1 Unit tests

- Pure feature, agent, decision, risk, exit, fill, accounting, and report calculations.
- Boundary and invalid-input cases for every threshold and reason code.

### 17.2 Property and invariant tests

- Cash plus marked position value reconciles to equity under the margin model.
- Filled quantity never exceeds requested, available, or open reduce-only quantity.
- Fees and funding never disappear across partial closes.
- Counterfactual events cannot mutate official account tables.
- Stops never move away from risk.
- Event stream versions are gapless per aggregate.
- Decision cycles are exactly once.

### 17.3 Contract tests

- Market-data, clock, execution, event-store, notification, and control adapters.
- Binance production and demo endpoints are distinct and enforced by adapter type.
- Exchange filters quantize and reject correctly.

### 17.4 PostgreSQL integration tests

- Migrations from empty database to head.
- ORM and migration schema agreement.
- Atomic event/projection/outbox behavior.
- Concurrent command and event optimistic locking.
- Restart reconstruction from persisted state.

### 17.5 Deterministic replay tests

- Frozen market fixture drives complete decision, order, fill, exit, and report paths.
- Golden normalized event stream and final account state are repeatable.
- Look-ahead tests prove future events cannot affect prior decisions or fills.

### 17.6 Fault injection

- WebSocket disconnect, missing bars, duplicate events, reordered events, stale book, venue error, database outage, disk-full simulation, worker kill, API kill, and clock drift.
- Recovery must produce no duplicate decision, order, fill, or report.

### 17.7 Browser tests

- Mission Control rendering and live updates.
- Audit drill-down completeness and filters.
- Report reconciliation.
- Control confirmation, idempotency, and visible command results.

### 17.8 Static and CI gates

- Ruff formatting and lint.
- Pyright strict type checking for new platform modules, expanded to legacy modules as corrected.
- Test coverage thresholds by critical package, with paper broker, ledger, risk, and recovery branches held to the highest threshold.
- Dependency and secret scanning.
- CI provisions PostgreSQL and runs migrations, tests, lint, typing, frontend tests, and production-endpoint safety tests.

## 18. Operational Platform

- Docker Compose uses pinned image and package versions.
- PostgreSQL and application data use named volumes.
- Services expose health and readiness probes.
- Structured JSON logs contain run ID, correlation ID, symbol, component, and event ID.
- Metrics include ingestion lag, decision duration, queue depth, data quarantines, reconnects, order state, P&L, exposure, incidents, and report freshness.
- Backups run daily to a local directory outside the database volume.
- A restore drill is required before the 24-hour soak.
- The repository contains setup, start, stop, reset-paper-data, backup, restore, replay, preflight, and runbook documentation.
- `.superpowers/` is ignored and remains design-session scratch space.

## 19. Delivery Phases

### Phase 0 - Baseline safety and repository health

- Correct stale documentation and establish one authoritative roadmap/status.
- Add CI, Docker Compose, type checking, formatting, and clean lint.
- Introduce explicit run modes and production-endpoint safety tests.
- Correct current execution approval and compliance fail-open defects before reuse.

### Phase 1 - Event ledger and experiment model

- Add schema, migrations, repositories, atomic event/projection/outbox behavior, and complete decision lineage.

### Phase 2 - Paper broker, account, and exits

- Implement clock, filters, conservative fill engine, partial fills, funding, fees, margin, one-way positions, exit manager, and counterfactual isolation.

### Phase 3 - End-to-end orchestrator

- Wire validated live/replay market data through features, agents, decisions, monitoring, risk, paper execution, exits, and persistence.

### Phase 4 - Mission Control and reporting

- Add FastAPI query/control service, React dashboard, daily reports, exports, and reconciliation.

### Phase 5 - Resilience and operational hardening

- Add persistent halts, recovery, gap backfill, fault injection, backups, alerts, metrics, and runbooks.

### Phase 6 - Preflight and seven-day candidate

- Freeze configuration, run deterministic historical replay, production-endpoint safety tests, restore drill, and a 24-hour live-data soak. Demo/Testnet protocol smoke is optional, separately evidenced, and never a paper-readiness credential gate.
- Only after all gates pass is the system declared ready to begin the seven-day paper experiment.

### Phase 7 - Seven-day paper experiment

- Run continuously, generate daily reports, review incidents without mutating configuration, and produce the final experiment report.

## 20. Deliberately Deferred

- Real-money execution and production API credentials.
- Automatic intrarun or interrun weight application.
- Multi-user accounts, permissions, billing, and cloud deployment.
- Kafka or microservice decomposition without measured need.
- Indefinite storage of every 500 ms order-book update and individual tick.
- LLM authority over decisions or trading controls.
- Automatic strategy promotion or retirement.

These deferrals do not block the seven-day paper experiment and preserve explicit extension points.

## 21. References

- `MAAIS.pdf` and `BEHAVIOURS.md` define the product constraints.
- `ORCHESTRATOR.md` defines the batch approval and shipping process.
- Binance USD-M Futures Demo/Testnet: `https://demo-fapi.binance.com` and `wss://demo-fstream.binance.com`.
- Public live data may use production public REST/WebSocket endpoints; authenticated production order endpoints are absent from the paper-ready dependency graph.
