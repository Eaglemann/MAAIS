# MAAIS Phase 3 Validated Market Data and Orchestrator Plan

> **Execution:** Follow this plan test-first. Phase 3 completes the deterministic live/replay worker, but it does not declare the system ready for a week-long paper run. Mission Control, operational hardening, and the 24-hour soak remain separate gates.

**Goal:** Process each configured closed one-minute bar exactly once through causal market-data validation, features, all eight agent records, the ordered decision/risk gates, local paper execution or isolated counterfactual tracking, protective exits, and atomic PostgreSQL events.

**Architecture:** Immutable observed market events enter a bounded, loss-intolerant ingestion boundary. A deterministic frame builder emits one causal market frame per experiment, symbol, timeframe, and bar close. A tri-state integrity state machine either admits the frame, quarantines it, or holds it in recovery. A pure orchestration service builds the complete typed decision and execution result. PostgreSQL commits projections and event/outbox streams through one unit of work. Replay and live-public-data adapters implement the same ports.

**Tech stack:** Python 3.12, asyncio, frozen dataclasses, Decimal at financial boundaries, HTTPX, WebSockets, SQLAlchemy async, PostgreSQL 16, Alembic, pytest, Hypothesis, Ruff, Pyright, and public unauthenticated venue APIs.

## Execution progress

- 2026-08-02: Tasks 1-3 pure contracts completed for immutable observed events, causal exactly-once frames, and mandatory tri-state integrity admission. Public adapters and PostgreSQL persistence remain pending, so this is not yet a runnable live worker.
- 2026-08-02: Revision `0009` now persists normalized quality rows, ingestion cursors, recovery runs, incidents, and worker checkpoints with event/outbox streams, optimistic versions, restart loaders, ledger checks, rollback/concurrency coverage, and a verified downgrade/upgrade drill. REST recovery orchestration and the live worker remain pending.
- 2026-08-02: The official agent matrix now always returns eight ordered visible rows with manifest maturity, proxy labels, deterministic or monotonic durations, input contributions, and blocking failure metadata. Pure official monitoring and risk gates now fail closed on price/stop, Kelly, correlation warmup, portfolio loss at stop, drawdown, health, black-swan warmup, liquidity, and benchmark provenance. Atomic orchestration remains pending.
- 2026-08-02: The pure orchestrator now produces deterministic quarantine and mandatory-agent-failure bundles with incidents, plus admitted neutral, rejected-counterfactual, approved-executed, and approved-but-unfillable halt outcomes. Directional decisions use Decimal consensus, adversarial, cost, EV, benchmark, monitoring, risk, exchange, and broker-capacity gates; successful entries carry exact gate-hash authorization, paper fills, reconciled account/exit state, and sensitivity scenarios. Atomic outcome persistence, protective-exit driving, funding lifecycle, public adapters, and worker resume remain pending.
- 2026-08-02: Atomic outcome persistence and restart loaders are complete. Revision `0010` persists restartable stop/target trigger reason, venue/local timing, trigger price, executable price, and `Regular`/`Special` funding lineage. Protective marks continue while entries are halted; successful exits and funding reconcile through restart, while unfillable exits atomically persist the triggered plan, critical incident, and terminal experiment halt. Public adapters and worker lifecycle remain pending.
- 2026-08-02: Strict, keyless public adapters now cover Binance USD-M REST/WebSocket, Binance Spot primary references, and Bybit Spot secondary references for all ten admitted symbols. They retain venue identity, time, sequence availability, executable quote metadata, and fail closed on schema drift, gaps, queue saturation, or unreconciled depth. Binance Spot uses the standard official public origin because a live check found the market-data-only origin too stale for the five-second admission limit. REST closed-bar recovery coordination and the worker lifecycle remain pending.
- 2026-08-02: Closed-bar gap recovery now persists detection before public REST I/O, fetches and validates the exact missing range, records bounded retry attempts with exponential backoff, and terminally fails after exhaustion. Completion locks PostgreSQL and requires the exact candidate cursor already be durable; the worker still must dispatch recovered bars through normal frame/cycle transactions before calling completion.

## Current defects this phase must retire

- The legacy integrity validator logs failures but does not control admission.
- A zero spot/reference price is currently treated as a passing skipped check.
- Futures-versus-Binance-spot basis is mislabeled as cross-exchange validation.
- API-outage checks read wall-clock time internally and are not replayable.
- Live event schemas do not consistently retain venue time, observed time, source sequence, and event identity.
- The WebSocket connector can drop events when its queue fills and only logs the loss.
- Some depth parsing falls back to local wall-clock time and a zero sequence.
- Reconnect does not prove cursor continuity or backfill missing closed bars before resuming decisions.
- The agent runner omits incompatible agents even though every cycle must contain all eight rows.
- Agent timing and maturity/proxy labeling are not part of the authoritative runner output.
- Risk sizing uses `zscore_mean` as a price proxy.
- Correlation warm-up can grant full allocation without 60 aligned returns.
- Monitoring does not require every mandatory component to be fresh and healthy before entry.
- Benchmark return can default to zero instead of using an explicit benchmark observation.
- No worker checkpoint, ingestion cursor, recovery-run, or incident projection exists.
- There is no end-to-end worker/CLI that owns graceful stop, restart, resume, and exactly-once cycle dispatch.

## Fixed Phase 3 invariants

- Live paper mode uses only public unauthenticated market-data endpoints and never accepts exchange credentials.
- Every event has immutable venue, stream, symbol, event ID, venue timestamp, local observed timestamp, and source sequence or an explicit sequence-not-applicable reason.
- No parser invents a venue timestamp, event ID, or sequence from the local clock.
- Queue capacity loss, parse ambiguity, sequence gaps, duplicate conflicts, and cursor regression halt admission and create incidents.
- A frame uses only events observed at or before its cutoff. Later data cannot revise an admitted official frame.
- A frame key is exactly `(experiment_id, symbol, timeframe, bar_close_at, strategy_version_id)` and produces at most one decision cycle.
- Identical frame/cycle retries are idempotent. Different content under the same key is a conflict and persistent halt.
- Required integrity checks return `passed`, `failed`, or `not_applicable`; required `not_applicable` blocks admission.
- Futures/spot basis and true secondary-venue divergence are separate checks.
- Every official symbol has an explicit secondary-reference mapping. Missing mappings block experiment admission.
- Gap recovery blocks new entries, backfills exact missing closed bars, recomputes downstream frames deterministically, and resumes only after cursor catch-up and validation.
- Every cycle contains exactly the configured eight agent evaluations, including incompatible and disabled neutral rows with reason codes.
- Macro sentiment remains visibly `proxy` until a versioned real macro/news input exists.
- Features and agents read only the admitted frame and prior warmed history.
- Current executable mark/mid price is the sizing price. Statistical means are never prices.
- Nonpositive Kelly, insufficient correlation history for multi-symbol exposure, cold black-swan baselines, unhealthy components, and absent benchmarks block entries.
- The complete gate chain is ordered, immutable, persisted, and bound into execution authorization.
- A decision bundle and any resulting paper execution/counterfactual/incident mutations commit atomically where they share a cycle.
- Exit evaluation continues even while new entries are quarantined or halted.
- Replay-normalized events and projections are byte-identical from the same frozen inputs.

## Planned files

### Market data and validation

- `maais/market_data/events.py` - immutable observed event envelopes and normalized payloads.
- `maais/market_data/frames.py` - causal one-minute frame builder and exactly-once frame keys.
- `maais/market_data/integrity/state_machine.py` - required tri-state checks and quarantine decision.
- `maais/market_data/reference.py` - true secondary-reference and spot-basis ports.
- `maais/market_data/recovery.py` - gap detection, backfill, recomputation, and recovery state.
- `maais/market_data/connectors/binance_rest.py` - verified public Futures REST adapter.
- `maais/market_data/connectors/binance_websocket.py` - bounded public Futures stream adapter.
- `maais/market_data/connectors/binance_spot.py` - verified primary Spot reference adapter.
- `maais/market_data/connectors/bybit_spot.py` - verified secondary-venue reference adapter.

### Decision, risk, and orchestration

- `maais/agents/evaluations.py` - exactly-eight execution matrix with maturity and timing metadata.
- `maais/risk/official.py` - Decimal official sizing from executable price and loss at stop.
- `maais/monitoring/admission.py` - mandatory health, warm-up, drawdown, and halt gate.
- `maais/orchestration/commands.py` - immutable frame/event commands.
- `maais/orchestration/results.py` - typed admitted, quarantined, neutral, rejected, executed, and halted results.
- `maais/orchestration/service.py` - pure ordered orchestration pipeline.
- `maais/orchestration/worker.py` - live/replay lifecycle, dispatch, stop, and resume.
- `maais/cli.py` - explicit replay and public paper-live commands.

### Persistence

- `maais/db/models/operations.py` - cursors, recoveries, incidents, and worker checkpoints.
- `maais/db/repositories/market_data.py` - frame/cursor/recovery writes.
- `maais/db/repositories/incidents.py` - idempotent halt/incident writes.
- `maais/db/repositories/orchestration.py` - atomic cycle outcome coordinator.
- `alembic/versions/0009_orchestrator.py` - Phase 3 schema and constraints.
- `alembic/versions/0010_exit_trigger_state.py` - restartable protective-exit trigger state.

### Tests and evidence

- `tests/unit/market_data/test_observed_events.py`
- `tests/unit/market_data/test_frame_builder.py`
- `tests/unit/market_data/test_integrity_state_machine.py`
- `tests/unit/market_data/test_gap_recovery.py`
- `tests/unit/orchestration/test_agent_matrix.py`
- `tests/unit/orchestration/test_official_risk.py`
- `tests/unit/orchestration/test_service.py`
- `tests/integration/test_orchestration_repository.py`
- `tests/integration/test_worker_resume.py`
- `tests/replay/test_golden_orchestrator.py`
- `artifacts/readiness/phase-3-verification.json` - ignored direct evidence.

## Task 1 - Immutable observed market-event contracts

- [ ] Write validation tests for UTC, Decimal, uppercase symbol, nonempty venue/stream/event ID, observed chronology, and monotonic sequence.
- [ ] Define closed-bar, book, trade, mark/funding, venue-clock, symbol-state, futures-spot, and secondary-reference events.
- [ ] Preserve venue event time and local observed time separately.
- [ ] Require deterministic content hashes and canonical event IDs.
- [ ] Reject float financial values and any missing mandatory source field.
- [ ] Adapt replay fixtures first; retain legacy schemas only behind a compatibility adapter.

Gate: event normalization is pure, byte-stable, and has no network or database dependency.

## Task 2 - Causal exactly-once one-minute frames

- [ ] Write tests proving only fully closed aligned one-minute bars dispatch.
- [ ] Prove books, marks, funding, references, and symbol state after the cutoff are unavailable.
- [ ] Build a frame from the last eligible observations and record every source event ID/sequence.
- [ ] Reject duplicate-conflicting bars, nonmonotonic sequences, and crossed or stale books.
- [ ] Produce a deterministic frame key and content hash.
- [ ] Prove identical event order permutations normalize to the same frame while causal differences change the hash.

Gate: one frozen input stream produces one canonical frame and later-event mutations do not alter it.

## Task 3 - Mandatory integrity state machine

- [ ] Replace boolean/logging admission with typed check results and required/optional policy.
- [ ] Implement missing interval, duplicate, sequence, venue timestamp, observed lag, local clock drift, stale source, API outage, OHLC, nonnegative volume, crossed book, symbol trading state, and historical coverage checks.
- [ ] Implement Decimal close-return outlier checks with an explicit warm-up state.
- [ ] Implement futures-versus-spot basis as its own check.
- [ ] Implement true secondary-venue divergence through `ReferencePricePort`.
- [ ] Treat missing required reference, clock, symbol, or history inputs as blocking `not_applicable`, never passing.
- [ ] Emit a complete quality snapshot and quarantine reason set for every frame.

Gate: any failed or required-not-applicable check creates a quarantined cycle and cannot reach features or an executable proposal.

## Task 4 - Deterministic gap recovery

- [x] Detect exact missing closed-bar intervals from persisted cursors.
- [ ] Enter a recovery state that blocks new entries but continues protective exits.
- [x] Fetch exact REST ranges through an injected backfill port.
- [x] Validate response bounds, duplicates, order, coverage, and closed status.
- [ ] Rebuild affected derived frames from the earliest changed interval.
- [x] Persist recovery start, attempts, source hashes, completion, failure, and cursor movement.
- [ ] Resume only when live and recovered cursors meet without unexplained gaps.

Gate: crash/reconnect fixtures recover without duplicate frames or cycles and with identical normalized hashes.

## Task 5 - Exactly-eight agent evaluation matrix

- [x] Replace silent regime filtering with one row for every configured agent.
- [x] Record compatible, enabled, maturity, proxy label, reason codes, input contributions, duration, and output.
- [x] Use deterministic injected timing in replay and monotonic timing in live mode.
- [x] Convert assertion-only agent validation into runtime exceptions.
- [x] Force incompatible and disabled agents to neutral nonvoting rows.
- [x] Prove a throwing, missing, duplicated, or malformed mandatory agent blocks the cycle and creates an incident.

Gate: every non-quarantined cycle has exactly eight unique agent names and replay-stable authoritative outputs.

## Task 6 - Official monitoring and risk corrections

- [x] Size from current executable mark/mid and actual stop distance, not `zscore_mean`.
- [x] Reject zero or negative Kelly before any quantity calculation.
- [x] Require 60 aligned returns before multi-symbol correlation can admit exposure.
- [x] Sum portfolio loss at stop independently from gross-notional and margin caps.
- [x] Make drawdown/peak inputs explicit and restorable.
- [x] Require warm rolling black-swan baselines per symbol/timeframe.
- [x] Require fresh health for every mandatory component.
- [x] Require an explicit benchmark observation and reject a default-zero benchmark.
- [x] Return a typed gate result for every pass/fail with exact inputs and reason code.

Gate: red tests cover every historical permissive default and prove it now fails closed.

## Task 7 - Pure ordered orchestration service

- [x] Define one command from an admitted or quarantined frame plus manifest-pinned dependencies.
- [x] For quarantine, persist the frame, eight neutral/nonvoting agent rows, a quarantined decision, data-quality gate, and incident without running features.
- [x] For admitted frames, compute features, eight evaluations, consensus, adversarial result, costs, EV, benchmark alpha, and all ordered gates.
- [x] Produce neutral cycles without proposals.
- [x] Produce rejected directional proposals plus isolated counterfactual state.
- [x] Produce approved proposals only after monitoring, drawdown, correlation, portfolio-risk, leverage, exchange-filter, and broker-capacity gates.
- [x] Issue a short-lived execution capability bound to the exact persisted gate-chain hash.
- [x] Apply official paper fills, sensitivities, account, exit plans, and funding.
- [x] Evaluate protective exits on every eligible mark even when entry admission is halted.
- [ ] Convert unfillable exits and compliance/persistence failures into persistent experiment halts.

Gate: no public method can call the broker with a raw unapproved quantity or bypass a required gate.

## Task 8 - PostgreSQL orchestration state and atomicity

- [x] Add revision `0009` for market cursors, quality rows, recovery runs, incidents, and worker checkpoints.
- [x] Add uniqueness/check constraints for cursor monotonicity, one frame/cycle key, active recovery, incident identity, and checkpoint versions.
- [x] Persist frame, quality results, eight evaluations, decision, gates, proposal, counterfactual/execution result, incident, cursor, and outbox atomically where applicable.
- [x] Add optimistic concurrency for worker checkpoints and incident transitions.
- [x] Add restart loaders for cursors, active recoveries, pending orders, open positions, exit plans, and unresolved counterfactuals.
- [x] Extend ledger consistency checks across new projections.

Gate: rollback, concurrent duplicate, conflicting retry, and restart tests pass on PostgreSQL revision `0010`.

## Task 9 - Verified public-data adapters

- [x] Re-check current official venue documentation before implementing message fields, sequence rules, rate limits, and endpoints.
- [x] Keep public live REST/WebSocket origins explicit and disjoint from authenticated Demo smoke execution.
- [x] Remove parser wall-clock fallbacks and silent defaults.
- [x] Use bounded backpressure; queue saturation creates a halt/incident instead of dropping data.
- [x] Retain and await connector tasks; expose deterministic start, ready, stop, and failure states.
- [ ] Implement ping/pong, reconnect backoff with jitter, cursor reconciliation, and REST backfill.
- [x] Add a true secondary-reference adapter for every admitted symbol mapping.
- [x] Add contract fixtures from sanitized official payload shapes and parser mutation tests.

Gate: public live mode starts with no API keys, and every disconnect or sequence gap follows the recovery state machine.

## Task 10 - Worker lifecycle, resume, and CLI

- [ ] Add `replay` and `paper-live` commands with explicit experiment/manifest identity.
- [ ] Enforce one worker lease per experiment.
- [ ] Start from a reconciled database checkpoint, never an assumed empty account.
- [ ] Handle SIGINT/SIGTERM with ingestion stop, queue drain, in-flight transaction completion, checkpoint, and connector close.
- [ ] Resume pending/partial orders, open exits, recoveries, cursors, and counterfactuals idempotently.
- [ ] Fail startup on schema drift, manifest mismatch, unreconciled account, invalid references, or unsafe run mode.
- [ ] Print a concise local operator status and authoritative experiment ID.

Gate: kill-and-restart tests produce no duplicate frame, cycle, order, fill, funding, counterfactual, or event.

## Task 11 - Golden replay and Phase 3 evidence

- [ ] Build a frozen multi-symbol stream with admitted, neutral, rejected, approved, partial-fill, funding, exit, quarantine, gap, recovery, and halt paths.
- [ ] Replay it twice and assert byte-identical normalized event streams and final projections.
- [ ] Mutate future events and prove prior frames, decisions, and fills are unchanged.
- [ ] Run Ruff, Pyright, secret scan, dependency audit, full tests, PostgreSQL integration, migrations, and ledger reconciliation.
- [ ] Record schema, counts, replay hashes, restart hashes, incident paths, limitations, and worktree identity in ignored evidence.

## Incremental checkpoint policy

Create local commits after each green tranche rather than one large final change:

1. Phase 3 plan.
2. Observed events, causal frames, and integrity admission.
3. Cursor, incident, recovery persistence, and migration `0009`.
4. Exactly-eight agents and monitoring/risk corrections.
5. Pure replay orchestrator and atomic repository.
6. Verified public adapters, worker lifecycle, and CLI.
7. Golden replay and Phase 3 evidence.

Push remains withheld while the configured remote is public unless the repository owner explicitly approves publication.

## Phase 3 definition of done

- Frozen input events reproduce identical frames, decisions, gates, executions, exits, counterfactuals, incidents, cursors, and final state.
- Public live-data mode runs without any exchange credential.
- Failed or unavailable required data never feeds a tradable proposal.
- Every cycle has exactly eight visible agent rows.
- Price, benchmark, correlation, drawdown, black-swan, and health controls fail closed.
- Gate order and execution authorization cannot be bypassed.
- Restart and reconnect do not duplicate authoritative work.
- Phase 3 evidence explicitly states that Mission Control, resilience drills, runbooks, and the 24-hour soak are still pending.
