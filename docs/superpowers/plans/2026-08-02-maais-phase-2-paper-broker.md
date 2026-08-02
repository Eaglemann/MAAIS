# MAAIS Phase 2 Paper Broker, Account, and Exit Plan

> **Execution:** Follow this plan test-first with `superpowers:executing-plans`. Phase 2 is not a readiness declaration; it only establishes the deterministic execution and accounting substrate required by the orchestrator.

**Goal:** Turn approved decision proposals into reproducible local paper orders, fills, positions, exits, account state, and isolated counterfactual outcomes without authenticated exchange access or look-ahead information.

**Architecture:** Pure Decimal domain services calculate clock eligibility, exchange filtering, fills, positions, exits, fees, funding, and sensitivity outcomes from immutable observed market events. PostgreSQL persists order/account aggregates and their domain events atomically. Official account mutation and counterfactual research use separate repositories and schemas so a hypothetical trade cannot affect equity or risk.

**Tech stack:** Python 3.12, frozen dataclasses, Decimal, HMAC-SHA256 capabilities, SQLAlchemy async, PostgreSQL 16, Alembic, pytest, Hypothesis, Ruff, and Pyright.

## Fixed safety and realism constraints

- `PAPER_LIVE` execution never requires or accepts Binance API credentials.
- Signed exchange execution remains confined to `TESTNET_SMOKE` and the pinned Binance Demo endpoint.
- UTC is authoritative. An order can use only a market event observed strictly after both decision completion and configured latency.
- All monetary, price, quantity, rate, fee, and P&L math uses finite `Decimal`; floats are rejected at new paper-platform boundaries.
- Exchange filters are immutable snapshots in an experiment manifest and copied to each intent.
- Quantity is quantized down. Price is rounded conservatively by side and order purpose. Quantization never increases approved risk.
- Market fills consume recorded visible depth from the correct side of the eligible book. Stale or insufficient books reject; they never assume infinite liquidity.
- Limit touch is not a fill. Displayed size at the price is queue-ahead, then the order receives at most 10% of later qualifying aggressive volume per observed event.
- Requested, filled, and reduce-only quantities are hard upper bounds under partial fills and retries.
- Order status transitions are closed, monotonic, evented, and idempotent by client order ID plus command content hash.
- The first official account starts with exactly 10,000 USDT, uses 1x leverage, and hard-rejects leverage above 5x.
- Futures collateral accounting is: cash = initial capital + realized P&L - fees + funding; equity = cash + unrealized P&L; free margin = equity - used margin.
- There is at most one non-flat position per `(experiment, symbol)`. Opposite fills reduce FIFO lots before they can reverse.
- Every accepted filled entry gets a stop, target, 60-closed-one-minute-bar maximum hold, two-consecutive-approved-opposite-signal exit, and emergency flatten behavior.
- Stop execution may gap and uses the next eligible book after the trigger event.
- Fees and funding are allocated and retained across partial closes; reconciliation must hold after every mutation.
- Official account events and counterfactual events cannot share an account repository, foreign key, unit-of-work method, or table.
- Counterfactual outcomes use the same eligibility/fill/cost/exit policy, record 15m/1h/4h/24h horizons, and never affect official equity, risk, drawdown, learning, or statistics.
- Optimistic and stress sensitivity records are informational children of an official order, not alternative official fills.

## Planned files

### Pure domain

- `maais/execution/paper/clock.py` - deterministic clock and eligibility.
- `maais/execution/paper/filters.py` - exchange snapshots, quantization, validation.
- `maais/execution/paper/authorization.py` - HMAC capability issued only for an exact approved gate-chain hash.
- `maais/execution/paper/market.py` - immutable books, levels, and trade events.
- `maais/execution/paper/fills.py` - market-depth and conservative limit-queue models.
- `maais/execution/paper/orders.py` - order aggregate and state transitions.
- `maais/execution/paper/account.py` - collateral/equity/margin/reconciliation.
- `maais/execution/paper/positions.py` - one-way position and FIFO lots.
- `maais/execution/paper/exits.py` - stop, target, time, opposite-signal, and emergency exits.
- `maais/execution/paper/sensitivity.py` - optimistic/conservative/stress isolated calculations.
- `maais/research/counterfactuals.py` - hypothetical trade state and horizon outcomes.

### Persistence

- `maais/db/models/execution.py` - intents, events, fills, sensitivities.
- `maais/db/models/accounts.py` - positions, lots, exits, snapshots, funding entries.
- `maais/db/models/counterfactuals.py` - isolated research projection.
- `maais/db/repositories/execution.py` - idempotent order/fill persistence and events.
- `maais/db/repositories/accounts.py` - official account mutation and reconstruction.
- `maais/db/repositories/counterfactuals.py` - research-only writes with no account dependency.
- `alembic/versions/0007_paper_execution.py` - execution/account schema and constraints.
- `alembic/versions/0008_counterfactuals.py` - mechanically isolated counterfactual schema.

### Tests

- `tests/unit/paper/test_clock_filters_authorization.py`
- `tests/unit/paper/test_market_fills.py`
- `tests/unit/paper/test_limit_queue.py`
- `tests/unit/paper/test_account_positions.py`
- `tests/unit/paper/test_exits.py`
- `tests/unit/paper/test_counterfactuals.py`
- `tests/property/test_paper_invariants.py`
- `tests/integration/test_paper_execution_repository.py`
- `tests/integration/test_account_repository.py`
- `tests/integration/test_counterfactual_isolation.py`
- `tests/integration/test_paper_reconstruction.py`

## Task 1 - Clock, filters, and authorization

- [ ] Write tests proving an event at or before the eligibility timestamp is unavailable and the first later observed event is eligible.
- [ ] Implement `DeterministicClock` with injected current time and explicit `eligible_after(decided_at, latency)`.
- [ ] Write exchange-filter boundary tests for tick, step, min/max quantity, minimum notional, symbol state, and order type.
- [ ] Implement immutable `ExchangeFilterSnapshot`, downward quantity quantization, side-aware price quantization, and typed rejection reasons.
- [ ] Write tests proving a changed decision ID, proposal ID, gate-chain hash, quantity, expiry, or experiment invalidates authorization.
- [ ] Implement short-lived HMAC `ExecutionCapability`; never serialize the signing key or expose a public constructor for a valid token.

Gate: unit tests, Ruff, and Pyright pass; no paper execution adapter imports the authenticated client protocol.

## Task 2 - Market-depth fills

- [ ] Write frozen-book tests for buy ask-walk, sell bid-walk, exact VWAP, spread cost, depth slippage, and latency slippage.
- [ ] Write rejection tests for stale book, insufficient depth, wrong chronology, nonpositive levels, and future-event access.
- [ ] Implement immutable `BookLevel`, `BookSnapshot`, `TradePrint`, `FillSlice`, `PaperFill`, and `FillRejection` types.
- [ ] Implement conservative market fill walking only the eligible visible book with full cost decomposition.
- [ ] Prove deterministic equality from the same canonical input sequence.

Gate: no fill quantity exceeds requested or visible eligible depth, and no result depends on later events.

## Task 3 - Limit queue, partial fill, expiry, and order aggregate

- [ ] Write tests proving touch-without-trade produces no fill and queue-ahead must be consumed first.
- [ ] Write tests proving the 10% per-event participation cap, partial fills, multiple events, cancel, and expiry.
- [ ] Implement immutable limit-queue state advanced by qualifying aggressive trades in timestamp/sequence order.
- [ ] Implement `PaperOrder` transitions: created, authorized, accepted, partially-filled, filled, canceled, rejected, expired.
- [ ] Reject illegal terminal transitions, overfill, changed-content retries, and reduce-only quantity above open quantity.
- [ ] Produce canonical order events with deterministic sequences and hashes.

Gate: property tests prove monotonic filled quantity and terminal-state immutability.

## Task 4 - Position and account calculations

- [ ] Write long and short FIFO examples with partial open, scale-in, partial close, full close, and attempted reversal.
- [ ] Implement one-way `PositionState`, immutable FIFO lots, exact average entry, realized/unrealized P&L, fee allocation, and funding allocation.
- [ ] Write 10,000 USDT account examples covering margin at 1x, fee debit, funding paid/received, mark-to-market, and insufficient free margin.
- [ ] Implement `AccountState` snapshots and a reconciliation report whose residual must be exactly zero at accounting precision.
- [ ] Hard-reject leverage below 1 or above 5; default and official policy remain 1x.
- [ ] Add Hypothesis sequences proving cash/equity/margin, FIFO, fee, funding, and quantity invariants.

Gate: every accepted mutation returns a reconciled account and never creates two open directions for one symbol.

## Task 5 - Exit manager

- [ ] Write long/short stop and target boundary tests using mark-price triggers.
- [ ] Write maximum-hold tests based on 60 closed one-minute bars after first fill, not wall-clock guesses.
- [ ] Write two-consecutive-approved-opposite-decision tests with resets for neutral/rejected cycles.
- [ ] Implement immutable versioned exit plans from actual average fill and expected loss/gain distances.
- [ ] Resize plans after partial entries and closes; prove a stop can move toward safety but never away from risk.
- [ ] Implement emergency flatten intents as reduce-only market orders for exact open quantity.
- [ ] Treat inability to execute a triggered exit as a persistent halt/incident result for later orchestration.

Gate: all open positions have an active plan and every exit intent is reduce-only and bounded.

## Task 6 - PostgreSQL order and account persistence

- [ ] Add revision `0007` with UUID keys, UTC timestamps, numeric fields, JSONB snapshots, uniqueness, check constraints, and one-open-position partial unique index.
- [ ] Add immutable ORM models matching the migration schema.
- [ ] Implement one unit-of-work transaction for intent/event/fill/position/lot/exit/account projections plus matching domain/outbox events.
- [ ] Make client order ID retries idempotent only for identical command hashes.
- [ ] Add optimistic versions to orders, positions, exits, and account aggregates.
- [ ] Add integration tests for partial fills, concurrent retries, rollback, restart reconstruction, event/projection reconciliation, and exact account totals.

Gate: revision `0007`, integration suite green, and replay reconstructs normalized order/account state.

## Task 7 - Counterfactual isolation and sensitivity

- [ ] Write static/API tests proving `CounterfactualRepository` cannot receive or construct `AccountRepository`.
- [ ] Add revision `0008` with counterfactual and horizon-outcome tables that have no foreign key to account, position, lot, fill, or snapshot tables.
- [ ] Implement the same eligibility/fill/fee/funding/exit assumptions through pure calculation inputs and research-only persistence.
- [ ] Record rejection gate, prior gate chain, entry existence, hypothetical costs, MFE, MAE, 15m/1h/4h/24h outcomes, exit reason, P&L, and resolution state.
- [ ] Implement optimistic and stress sensitivity outcomes as isolated projections linked to the source intent.
- [ ] Prove counterfactual transactions leave every official account table and official domain stream unchanged.

Gate: database-isolation tests compare official table counts and account hashes before/after hypothetical processing.

## Task 8 - Deterministic broker facade and Phase 2 evidence

- [ ] Implement `PaperBroker` as a pure/event-producing facade composed from clock, filter, authorization, fill, order, account, and exit services.
- [ ] Require complete decision/proposal lineage and a verified capability before an order is accepted.
- [ ] Add a golden frozen sequence covering accepted market fill, partial limit fill, cancel, funding, stop gap, rejected proposal counterfactual, and final account.
- [ ] Replay the same sequence twice and assert byte-identical canonical events and normalized final projections.
- [ ] Add look-ahead mutation tests showing later book/trade changes cannot alter earlier decisions or fills.
- [ ] Update CI integration schema assertions to `0008` and run the full security/quality/database gate.
- [ ] Record ignored evidence in `artifacts/readiness/phase-2-verification.json` with exact counts, hashes, schema, reconciliation, and known limitations.

## Phase 2 definition of done

- Frozen observed inputs reproduce identical orders, fills, account, exits, and counterfactual outputs.
- A fill cannot use future, stale, hidden, or insufficient liquidity.
- The official account reconciles after partial opens/closes, fees, funding, and stop gaps.
- Official and counterfactual persistence are mechanically isolated.
- All execution is local paper simulation; no credential or authenticated production path is involved.
- Phase 2 evidence is green and explicitly states that the live orchestrator, dashboard, operational hardening, and 24-hour soak are still pending.
