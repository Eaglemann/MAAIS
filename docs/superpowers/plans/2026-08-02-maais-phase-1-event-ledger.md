# MAAIS Phase 1 Event Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the immutable experiment identity, optimistic event streams, atomic outbox/projections, and complete exactly-once decision lineage that all broker, dashboard, recovery, and reporting work can trust.

**Architecture:** PostgreSQL owns an append-only event stream and normalized query projections in one transaction. An `event_streams` lock row serializes each aggregate without serializing unrelated aggregates; `domain_events`, projections, and `outbox_events` commit through one async unit of work. Frozen manifests and complete decision bundles are validated in pure domain code before persistence.

**Tech Stack:** Python 3.12, dataclasses, Decimal, SHA-256, SQLAlchemy 2 async, PostgreSQL 16 JSONB/UUID/identity columns, Alembic, pytest, pytest-asyncio, Hypothesis, Ruff, Pyright.

## Global Constraints

- PostgreSQL is authoritative for experiments, decisions, gates, events, projections, and outbox state.
- Every operational primary key is UUID; monotonic `BIGINT IDENTITY` columns are technical cursors only.
- All operational timestamps are timezone-aware UTC `timestamptz`.
- JSON snapshots carry an explicit schema version and are canonicalized before hashing.
- Event stream versions start at one and remain gapless per `(aggregate_type, aggregate_id)`.
- Appending requires an exact expected version; conflicts fail before any projection or outbox mutation commits.
- `domain_events` is append-only at both repository and PostgreSQL-trigger levels.
- One unit of work atomically appends events, updates projections, and inserts one outbox row per event.
- Experiment manifests are immutable after creation; lifecycle transitions change typed projection fields, never manifest JSON/hash.
- Supported modes remain exactly `REPLAY`, `PAPER_LIVE`, and `TESTNET_SMOKE`.
- Development manifests may include a dirty worktree hash; official soak/candidate manifests require a clean committed worktree.
- Every decision bundle contains exactly one evaluation for each of the eight configured agents.
- Disabled and incompatible agents remain explicit rows with reason codes.
- Gate sequences are contiguous from one, gate types do not repeat, and a failed gate prevents later passed gates.
- Decision uniqueness is `(experiment_id, symbol, timeframe, cycle_at, strategy_version_id)`.
- Retrying an identical completed decision is idempotent; retrying different content for the same decision key is a conflict.
- All probabilities/confidence/risk values are finite and in `[0, 1]`; weights are finite and positive.
- No commit, push, credential activation, timed run, or readiness declaration occurs without the later phase gates.

---

## File Structure

### Domain and experiments

- Create `maais/domain/ids.py` - UUID newtypes and UUIDv4 constructors.
- Create `maais/domain/enums.py` - lifecycle, maturity, decision, direction, disposition, gate, quality, and reason enums.
- Create `maais/domain/events.py` - immutable new/stored event envelopes and UTC validation.
- Create `maais/domain/json.py` - canonical JSON normalization and SHA-256 helpers.
- Create `maais/experiments/manifest.py` - frozen manifest, manifest factory, and clean-candidate validation.
- Create `maais/experiments/service.py` - allowed lifecycle transitions and event-producing commands.

### Persistence

- Create `maais/db/models/__init__.py` - model exports for Alembic discovery.
- Create `maais/db/models/ledger.py` - event-stream, domain-event, and outbox models.
- Create `maais/db/models/experiments.py` - experiment, strategy-version, and agent-version models.
- Create `maais/db/models/decisions.py` - market-frame, cycle, agent, summary, gate, and proposal models.
- Create `maais/db/repositories/events.py` - stream locking, append, load, and outbox behavior.
- Create `maais/db/repositories/experiments.py` - manifest and lifecycle projection persistence.
- Create `maais/db/repositories/decisions.py` - complete/idempotent decision-bundle persistence and reads.
- Create `maais/db/unit_of_work.py` - async transaction owner and repository access.
- Create `maais/db/replay.py` - stream/outbox consistency and experiment projection reconstruction.
- Create `alembic/versions/0005_event_ledger.py` - event and experiment schema plus mutation trigger.
- Create `alembic/versions/0006_decision_lineage.py` - complete decision-lineage schema and constraints.
- Modify `alembic/env.py` - import new model package.

### Verification

- Create `tests/unit/domain/test_events.py`.
- Create `tests/unit/experiments/test_manifest.py`.
- Create `tests/unit/experiments/test_lifecycle.py`.
- Create `tests/unit/decisions/test_bundle.py`.
- Create `tests/integration/conftest.py` - explicit `MAAIS_TEST_DATABASE_URL` fixture and targeted cleanup.
- Create `tests/integration/test_event_store.py`.
- Create `tests/integration/test_experiments.py`.
- Create `tests/integration/test_decision_lineage.py`.
- Create `tests/integration/test_replay_consistency.py`.
- Modify `.github/workflows/ci.yml` - PostgreSQL integration job and migration head assertion.
- Modify `pyproject.toml` - test marker and strict Pyright execution for the new modules.

## Fixed Interfaces

```python
@dataclass(frozen=True, slots=True)
class NewDomainEvent:
    aggregate_id: UUID
    aggregate_type: str
    event_type: str
    payload: Mapping[str, JsonValue]
    metadata: Mapping[str, JsonValue]
    occurred_at: datetime
    event_version: int = 1


@dataclass(frozen=True, slots=True)
class StoredDomainEvent(NewDomainEvent):
    id: UUID
    global_position: int
    stream_version: int


class OptimisticConcurrencyError(RuntimeError): ...


async def EventRepository.append(
    aggregate_id: UUID,
    aggregate_type: str,
    expected_version: int,
    events: Sequence[NewDomainEvent],
) -> tuple[StoredDomainEvent, ...]: ...


async def EventRepository.load_stream(
    aggregate_id: UUID,
    aggregate_type: str,
    after_version: int = 0,
) -> tuple[StoredDomainEvent, ...]: ...


@asynccontextmanager
async def UnitOfWork.begin() -> AsyncIterator[UnitOfWork]: ...


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: UUID
    name: str
    mode: RunMode
    initial_capital: Decimal
    currency: str
    created_at: datetime
    git_sha: str
    worktree_hash: str | None
    lock_hash: str
    schema_revision: str
    configuration: Mapping[str, JsonValue]
    symbols: tuple[str, ...]
    exchange_metadata: Mapping[str, JsonValue]
    component_versions: Mapping[str, str]
    agent_versions: tuple[AgentManifestEntry, ...]
    fee_policy: Mapping[str, JsonValue]
    funding_policy: Mapping[str, JsonValue]
    clock_policy: Mapping[str, JsonValue]
    market_data_sources: Mapping[str, JsonValue]
    manifest_schema_version: int = 1

    @property
    def config_hash(self) -> str: ...

    @property
    def manifest_hash(self) -> str: ...


async def DecisionRepository.record_bundle(bundle: DecisionBundle) -> DecisionRecordResult: ...


async def DecisionRepository.get_bundle(decision_cycle_id: UUID) -> DecisionBundleView: ...


async def verify_ledger_consistency(session: AsyncSession) -> LedgerConsistencyReport: ...
```

---

### Task 1: Canonical Domain Types, Enums, and Event Envelopes

**Files:**
- Create: `maais/domain/__init__.py`
- Create: `maais/domain/ids.py`
- Create: `maais/domain/enums.py`
- Create: `maais/domain/json.py`
- Create: `maais/domain/events.py`
- Test: `tests/unit/domain/test_events.py`

**Interfaces:**
- Consumes: `RunMode` from `maais.config.modes`.
- Produces: the fixed event envelope, canonical JSON hash, and closed persistence enums used by every later task.

- [ ] **Step 1: Write failing canonicalization and event tests**

```python
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.events import NewDomainEvent
from maais.domain.json import canonical_json_bytes, content_hash


def test_canonical_json_is_order_independent_and_lossless_for_decimal() -> None:
    left = {"b": Decimal("1.2300"), "a": [2, 1]}
    right = {"a": [2, 1], "b": Decimal("1.2300")}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert b'"1.2300"' in canonical_json_bytes(left)
    assert content_hash(left) == content_hash(right)


def test_event_rejects_naive_time_and_invalid_versions() -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        NewDomainEvent(
            aggregate_id=UUID(int=1),
            aggregate_type="experiment",
            event_type="experiment.created",
            payload={},
            metadata={},
            occurred_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValueError, match="event_version"):
        NewDomainEvent(
            aggregate_id=UUID(int=1),
            aggregate_type="experiment",
            event_type="experiment.created",
            payload={},
            metadata={},
            occurred_at=datetime.now(timezone.utc),
            event_version=0,
        )
```

- [ ] **Step 2: Run and observe import failures**

Run: `.venv/bin/pytest tests/unit/domain/test_events.py -q`

Expected: FAIL because `maais.domain` does not exist.

- [ ] **Step 3: Implement canonical JSON and UUID constructors**

`canonical_json_bytes(value)` recursively converts `Decimal` to its exact string, UTC datetime to ISO-8601 ending `Z`, UUID/Enum to string values, tuples to lists, mappings to sorted string keys, and rejects floats that are not finite. Serialize with `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` and UTF-8 encode. `content_hash` returns lowercase SHA-256 hex.

```python
ExperimentId = NewType("ExperimentId", UUID)
DecisionCycleId = NewType("DecisionCycleId", UUID)


def new_uuid() -> UUID:
    return uuid4()
```

- [ ] **Step 4: Implement exact enums**

Define string enums with lowercase stored values:

```python
ExperimentStatus = created, running, paused, stopped, completed, failed
StrategyStage = research, simulation, pilot, full_production
AgentMaturity = implemented, proxy, disabled
QualityStatus = passed, failed, not_applicable
DecisionStatus = completed, rejected, quarantined
Direction = long, short, neutral
Disposition = neutral, rejected, approved
ProposalStatus = neutral, rejected, approved, expired
GateType = data_quality, regime_compatibility, consensus, adversarial, ev,
           alpha, monitoring, drawdown, correlation, portfolio_risk,
           leverage, exchange_filters, paper_broker_capacity
ReasonCode = accepted, neutral_consensus, disabled_agent, incompatible_regime,
             data_quality_failed, insufficient_history, consensus_failed,
             adversarial_blocked, non_positive_ev, alpha_failed,
             monitoring_unhealthy, drawdown_halt, correlation_blocked,
             portfolio_risk_exceeded, leverage_rejected,
             exchange_filter_rejected, broker_capacity_rejected,
             duplicate_identical
```

- [ ] **Step 5: Implement frozen `NewDomainEvent` and `StoredDomainEvent`**

Validate non-nil UUID, dotted non-empty event/aggregate names, UTC-aware time, event version `>= 1`, stream/global versions `>= 1`, and canonicalizable payload/metadata in `__post_init__`.

- [ ] **Step 6: Run domain tests and static gates**

Run: `.venv/bin/pytest tests/unit/domain/test_events.py -q`

Run: `.venv/bin/ruff check maais/domain tests/unit/domain && .venv/bin/pyright maais/domain`

Expected: PASS and zero type errors.

### Task 2: Frozen Experiment Manifest and Lifecycle

**Files:**
- Create: `maais/experiments/__init__.py`
- Create: `maais/experiments/manifest.py`
- Create: `maais/experiments/service.py`
- Test: `tests/unit/experiments/test_manifest.py`
- Test: `tests/unit/experiments/test_lifecycle.py`

**Interfaces:**
- Consumes: `RunMode`, canonical JSON/hash, experiment lifecycle enums, `NewDomainEvent`.
- Produces: `ExperimentManifest`, `AgentManifestEntry`, `build_manifest`, `require_candidate_identity`, and `ExperimentLifecycle`.

- [ ] **Step 1: Write failing manifest identity tests**

```python
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from maais.config.modes import RunMode
from maais.experiments.manifest import build_manifest, require_candidate_identity


def test_manifest_hash_is_stable_and_manifest_is_frozen(manifest_inputs) -> None:
    first = build_manifest(**manifest_inputs)
    second = build_manifest(**dict(reversed(list(manifest_inputs.items()))))
    assert first.config_hash == second.config_hash
    with pytest.raises(FrozenInstanceError):
        first.name = "mutated"  # type: ignore[misc]


def test_candidate_requires_clean_commit(manifest_inputs) -> None:
    dirty = build_manifest(**manifest_inputs, worktree_hash="a" * 64)
    with pytest.raises(ValueError, match="clean committed worktree"):
        require_candidate_identity(dirty)


def test_manifest_requires_all_eight_unique_agents(manifest_inputs) -> None:
    with pytest.raises(ValueError, match="exactly eight"):
        build_manifest(**manifest_inputs, agent_versions=())
```

- [ ] **Step 2: Run and observe import failures**

Run: `.venv/bin/pytest tests/unit/experiments/test_manifest.py -q`

Expected: FAIL because experiment manifest types are absent.

- [ ] **Step 3: Implement frozen manifest and validation**

Use frozen/slotted dataclasses. Validate: capital positive Decimal; currency `USDT`; lowercase 40/64-character Git SHA accepted; all content hashes exactly 64 lowercase hex; non-empty unique symbols; exact `ALL_AGENTS`; positive finite weights; macro sentiment maturity is `PROXY` unless explicitly disabled; fee/clock/source policies non-empty. `config_hash` hashes normalized configuration only; `manifest_hash` hashes the complete normalized manifest.

- [ ] **Step 4: Write failing lifecycle transition tests**

```python
@pytest.mark.parametrize(
    ("status", "command", "next_status"),
    [
        (ExperimentStatus.CREATED, "start", ExperimentStatus.RUNNING),
        (ExperimentStatus.RUNNING, "pause", ExperimentStatus.PAUSED),
        (ExperimentStatus.PAUSED, "resume", ExperimentStatus.RUNNING),
        (ExperimentStatus.RUNNING, "stop", ExperimentStatus.STOPPED),
    ],
)
def test_valid_transition_emits_one_event(status, command, next_status, manifest) -> None:
    lifecycle = ExperimentLifecycle(manifest, status=status, version=3)
    transition = getattr(lifecycle, command)()
    assert transition.status is next_status
    assert transition.expected_version == 3
    assert len(transition.events) == 1


def test_completed_experiment_cannot_resume(manifest) -> None:
    lifecycle = ExperimentLifecycle(manifest, ExperimentStatus.COMPLETED, version=4)
    with pytest.raises(InvalidExperimentTransition):
        lifecycle.resume()
```

- [ ] **Step 5: Implement lifecycle transition table**

Allow only: created→running; running→paused/stopped/completed/failed; paused→running/stopped/failed. Every method returns `ExperimentTransition(status, expected_version, events)` with an event payload containing prior/new status, manifest hash, optional failure reason, and UTC event time.

- [ ] **Step 6: Run experiment unit tests and static gates**

Run: `.venv/bin/pytest tests/unit/experiments -q`

Run: `.venv/bin/ruff check maais/experiments tests/unit/experiments && .venv/bin/pyright maais/experiments`

Expected: PASS.

### Task 3: Event and Experiment Database Schema

**Files:**
- Create: `maais/db/models/__init__.py`
- Create: `maais/db/models/ledger.py`
- Create: `maais/db/models/experiments.py`
- Create: `alembic/versions/0005_event_ledger.py`
- Modify: `alembic/env.py`
- Test: `tests/integration/conftest.py`
- Test: `tests/integration/test_experiments.py`

**Interfaces:**
- Consumes: Base, domain enums, manifest fields.
- Produces: tables `event_streams`, `domain_events`, `outbox_events`, `experiments`, `strategy_versions`, `agent_versions`.

- [ ] **Step 1: Add explicit integration database fixture**

Read only `MAAIS_TEST_DATABASE_URL`; skip integration tests with a clear reason if absent; reject URLs whose database name does not end in `_test`. Create async engine/session factory. Before each test, delete from the six Phase 1 tables in foreign-key order inside the test database only. Never truncate the development `maais` database.

- [ ] **Step 2: Write failing schema-contract test**

```python
async def test_event_and_experiment_schema_contract(db_connection) -> None:
    tables = set(await table_names(db_connection))
    assert {
        "event_streams", "domain_events", "outbox_events", "experiments",
        "strategy_versions", "agent_versions",
    } <= tables
    assert await constraint_exists(db_connection, "uq_domain_event_stream_version")
    assert await constraint_exists(db_connection, "uq_outbox_domain_event")
```

- [ ] **Step 3: Implement models with exact keys and constraints**

`event_streams`: UUID `id`, `aggregate_id`, `aggregate_type`, integer `current_version`, created/updated UTC; unique aggregate pair. `domain_events`: UUID `id`, bigint identity `global_position`, stream FK, aggregate identifiers, positive stream/event versions, event type, JSONB payload/metadata, occurred/recorded UTC; unique stream version. `outbox_events`: UUID `id`, bigint identity `cursor`, unique event FK, topic, JSONB payload, created/published UTC, publish attempts/error. `experiments`: all design fields plus `manifest_schema_version`, immutable `manifest_json`, and unique `manifest_hash`; `config_hash` is indexed but repeatable across runs. Version tables use unique `(key/name, version)` so a reused semantic version with different content is a conflict.

- [ ] **Step 4: Implement migration and append-only trigger**

Migration `0005` creates the six tables, indexes on event type/time, aggregate/time, outbox unpublished cursor, experiment mode/status/time, and this PostgreSQL function/trigger:

```sql
CREATE FUNCTION maais_prevent_event_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'domain_events is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_domain_events_append_only
BEFORE UPDATE OR DELETE ON domain_events
FOR EACH ROW EXECUTE FUNCTION maais_prevent_event_mutation();
```

Downgrade removes trigger/function before tables.

- [ ] **Step 5: Upgrade the explicit test database**

Run: `MAAIS_TEST_DATABASE_URL=postgresql+psycopg://maais:maais@localhost:5432/maais_test .venv/bin/alembic upgrade head`

Expected: migrations through `0005` pass.

- [ ] **Step 6: Run schema contract and mutation-trigger tests**

Test direct `UPDATE domain_events` and `DELETE` each raise `DBAPIError` containing `append-only`. Run the schema test with `MAAIS_TEST_DATABASE_URL` set.

### Task 4: Optimistic Event Repository, Outbox, and Unit of Work

**Files:**
- Create: `maais/db/repositories/__init__.py`
- Create: `maais/db/repositories/events.py`
- Create: `maais/db/unit_of_work.py`
- Test: `tests/integration/test_event_store.py`

**Interfaces:**
- Consumes: event models and envelopes, async session factory.
- Produces: `EventRepository`, `OptimisticConcurrencyError`, and `UnitOfWork.begin()`.

- [ ] **Step 1: Write failing append/load/rollback tests**

```python
async def test_append_assigns_gapless_versions_and_outbox(uow_factory, events) -> None:
    async with uow_factory.begin() as uow:
        stored = await uow.events.append(AGGREGATE_ID, "experiment", 0, events[:2])
    assert [event.stream_version for event in stored] == [1, 2]
    async with uow_factory.begin() as uow:
        assert await uow.events.stream_version(AGGREGATE_ID, "experiment") == 2
        assert await uow.events.unpublished_outbox_count() == 2


async def test_stale_expected_version_rolls_back_projection_and_outbox(uow_factory, event) -> None:
    async with uow_factory.begin() as uow:
        await uow.events.append(AGGREGATE_ID, "experiment", 0, [event])
    with pytest.raises(OptimisticConcurrencyError):
        async with uow_factory.begin() as uow:
            await uow.events.append(AGGREGATE_ID, "experiment", 0, [event])
            await uow.experiments.set_status(AGGREGATE_ID, ExperimentStatus.RUNNING)
    assert await read_experiment_status() is ExperimentStatus.CREATED
    assert await outbox_count() == 1
```

- [ ] **Step 2: Implement per-stream lock algorithm**

Inside caller transaction: insert stream row with version zero using `ON CONFLICT DO NOTHING`; `SELECT ... FOR UPDATE`; compare current version exactly; assign sequential versions; flush each event to obtain global position; create one outbox row whose topic is event type and whose payload includes event ID/global/stream positions; update stream version. Convert unique races to `OptimisticConcurrencyError` without committing partial rows.

- [ ] **Step 3: Implement transaction-owning unit of work**

`UnitOfWork.begin()` creates a session and `session.begin()` scope, exposes repository instances sharing that session, commits only on clean exit, rolls back and re-raises on any exception, and always closes. Repositories never call commit.

- [ ] **Step 4: Add concurrency test**

Start two tasks with expected version zero for the same new stream, release them together with an `asyncio.Event`, and assert exactly one succeeds, one raises `OptimisticConcurrencyError`, final stream version is one, and exactly one event/outbox row exists. Repeat with different aggregate IDs and assert both succeed.

- [ ] **Step 5: Run event-store integration and static tests**

Run with `MAAIS_TEST_DATABASE_URL`: `.venv/bin/pytest tests/integration/test_event_store.py -q`

Run: `.venv/bin/ruff check maais/db tests/integration && .venv/bin/pyright maais/db`

Expected: PASS.

### Task 5: Experiment Repository and Immutable Manifest Projection

**Files:**
- Create: `maais/db/repositories/experiments.py`
- Test: `tests/integration/test_experiments.py`

**Interfaces:**
- Consumes: `ExperimentManifest`, lifecycle transitions, event repository in one UoW.
- Produces: `create`, `transition`, `get_manifest`, and version registration methods.

- [ ] **Step 1: Write failing creation/immutability tests**

```python
async def test_create_manifest_projection_and_event_are_atomic(uow_factory, manifest) -> None:
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    row = await load_experiment(manifest.experiment_id)
    assert row.config_hash == manifest.config_hash
    assert row.manifest_json == manifest.to_dict()
    assert await stream_version(manifest.experiment_id) == 1
    assert await outbox_count() == 1


async def test_lifecycle_transition_does_not_mutate_manifest(uow_factory, manifest) -> None:
    await create(manifest)
    original = await stored_manifest_json()
    await transition_to_running(expected_version=1)
    assert await stored_manifest_json() == original
    assert await experiment_status() == "running"
```

- [ ] **Step 2: Implement version registration and manifest create**

Register strategy/agent versions idempotently only when every stored field matches; a same natural key with different parameters raises `VersionIdentityConflict`. Insert experiment projection and append `experiment.created` expected version zero in one UoW. Persist exact canonical manifest JSON and config hash.

- [ ] **Step 3: Implement lifecycle transitions**

Lock experiment row; confirm current projection status and stream version match the `ExperimentLifecycle`; update only status/start/end/failure fields; append transition event with matching expected version. `started_at` is first transition to running and never changes on resume.

- [ ] **Step 4: Prove rollback and manifest immutability**

Force event append conflict after projection update and assert status rollback. Attempt SQLAlchemy changes to manifest/config hash after creation and have repository reject with `ImmutableManifestError` before flush.

- [ ] **Step 5: Run experiment integration tests**

Run with test URL: `.venv/bin/pytest tests/integration/test_experiments.py -q`

Expected: PASS.

### Task 6: Complete Decision-Lineage Schema and Bundle Validation

**Files:**
- Create: `maais/decisions/__init__.py`
- Create: `maais/decisions/bundle.py`
- Create: `maais/db/models/decisions.py`
- Create: `alembic/versions/0006_decision_lineage.py`
- Modify: `maais/db/models/__init__.py`
- Test: `tests/unit/decisions/test_bundle.py`
- Test: `tests/integration/test_decision_lineage.py`

**Interfaces:**
- Consumes: domain enums, `ALL_AGENTS`, experiment/strategy/agent version IDs.
- Produces: frozen market/cycle/agent/summary/gate/proposal DTOs and seven projection tables.

- [ ] **Step 1: Write failing bundle-completeness tests**

```python
def test_bundle_requires_exactly_all_agents(valid_bundle) -> None:
    incomplete = replace(valid_bundle, agents=valid_bundle.agents[:-1])
    with pytest.raises(ValueError, match="exactly one evaluation"):
        incomplete.validate()


def test_disabled_agent_must_explain_non_vote(valid_bundle) -> None:
    bad = replace(valid_bundle.agents[0], enabled=False, reason_codes=())
    with pytest.raises(ValueError, match="disabled_agent"):
        replace(valid_bundle, agents=(bad, *valid_bundle.agents[1:])).validate()


def test_gate_sequence_and_failure_are_fail_closed(valid_bundle) -> None:
    gates = (
        replace(valid_bundle.gates[0], sequence=1, passed=False),
        replace(valid_bundle.gates[1], sequence=2, passed=True),
    )
    with pytest.raises(ValueError, match="passed after failure"):
        replace(valid_bundle, gates=gates).validate()
```

- [ ] **Step 2: Implement frozen bundle DTOs and validation**

Create `MarketFrameRecord`, `DecisionCycleRecord`, `AgentEvaluationRecord`, `DecisionSummaryRecord`, `GateEvaluationRecord`, `TradeProposalRecord`, and `DecisionBundle`. Require UUID relationships, UTC timestamps, Decimal money/price fields, canonical JSON, finite bounded scores, exact agents, unique versions, contiguous gate order, status/disposition/direction coherence, and proposal presence for every directional cycle but absence for neutral cycles.

- [ ] **Step 3: Implement seven models and migration**

Create `market_frames`, `decision_cycles`, `agent_evaluations`, `decision_summaries`, `gate_evaluations`, and `trade_proposals` with every typed/JSON field in design section 6. Add `content_hash` to decision cycles for idempotency. Constraints: exact decision key unique; agent version unique per cycle; gate sequence and gate type unique per cycle; summary one-to-one; proposal one-to-one; score/weight/duration/non-negative checks; indexes on experiment/time/symbol/disposition/reason/gate/status.

- [ ] **Step 4: Upgrade test database to `0006` and verify ORM parity**

Use SQLAlchemy inspector to compare model table columns, nullable flags, PKs, unique constraints, and FKs with migrated schema for all Phase 1 models.

- [ ] **Step 5: Run bundle unit and schema integration tests**

Run: `.venv/bin/pytest tests/unit/decisions tests/integration/test_decision_lineage.py -q`

Expected: PASS.

### Task 7: Idempotent Complete Decision Repository and Bundle Read

**Files:**
- Create: `maais/db/repositories/decisions.py`
- Modify: `maais/db/unit_of_work.py`
- Test: `tests/integration/test_decision_lineage.py`

**Interfaces:**
- Consumes: validated `DecisionBundle`, event repository, decision models.
- Produces: `DecisionRecordResult(created, decision_cycle_id, content_hash)` and `DecisionBundleView`.

- [ ] **Step 1: Write failing complete-record/idempotency tests**

```python
async def test_record_bundle_is_complete_and_emits_events(uow_factory, valid_bundle) -> None:
    async with uow_factory.begin() as uow:
        result = await uow.decisions.record_bundle(valid_bundle)
    assert result.created
    assert await agent_count(result.decision_cycle_id) == 8
    assert await gate_count(result.decision_cycle_id) == len(valid_bundle.gates)
    assert await stream_is_gapless(result.decision_cycle_id)
    assert await event_count_for_cycle(result.decision_cycle_id) == 10 + len(valid_bundle.gates)


async def test_identical_retry_is_idempotent_but_changed_retry_conflicts(uow_factory, valid_bundle) -> None:
    first = await record(valid_bundle)
    second = await record(valid_bundle)
    assert not second.created
    assert second.decision_cycle_id == first.decision_cycle_id
    changed = replace(valid_bundle, cycle=replace(valid_bundle.cycle, reason_code="alpha_failed"))
    with pytest.raises(DecisionIdentityConflict):
        await record(changed)
```

- [ ] **Step 2: Implement deterministic bundle hash and decision-key claim**

Hash the complete normalized bundle. Insert the cycle key using PostgreSQL `ON CONFLICT DO NOTHING`; on conflict load existing ID/hash. Return idempotently only for exact hash equality; otherwise raise. The winner inserts all child projections and appends cycle, eight agent, all gate, and proposal events to the decision aggregate stream with one matching outbox per event.

- [ ] **Step 3: Implement complete bundle read**

Use explicit ordered queries or `selectinload`, never lazy ORM I/O. Return market frame, cycle, agents ordered by configured agent order, summary, gates by sequence, optional proposal, stream events, and manifest/config identity. Raise `IncompleteDecisionBundleError` if counts/relationships do not satisfy the same validation rules.

- [ ] **Step 4: Add concurrent identical/different retry tests**

Two identical concurrent bundles produce one projection set and one idempotent result. Two different bundles with the same key produce one success and one `DecisionIdentityConflict`; no mixed child rows exist.

- [ ] **Step 5: Run focused and full Phase 1 persistence tests**

Run with test URL: `.venv/bin/pytest tests/integration/test_decision_lineage.py -q`

Expected: PASS.

### Task 8: Replay Consistency, CI, and Phase Gate Evidence

**Files:**
- Create: `maais/db/replay.py`
- Create: `tests/integration/test_replay_consistency.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `pyproject.toml`
- Create: `artifacts/readiness/phase-1-verification.json` (ignored evidence)

**Interfaces:**
- Consumes: all streams, projections, outbox rows, manifests, decision bundles.
- Produces: `LedgerConsistencyReport(ok, stream_errors, outbox_errors, projection_errors)` and CI evidence.

- [ ] **Step 1: Write failing consistency/rebuild tests**

```python
async def test_consistency_report_accepts_valid_ledger(populated_ledger, session) -> None:
    report = await verify_ledger_consistency(session)
    assert report.ok
    assert not report.errors


async def test_consistency_report_finds_stream_and_outbox_damage(populated_ledger, session) -> None:
    await disable_trigger_and_delete_one_test_event(session)
    await delete_one_test_outbox_row(session)
    report = await verify_ledger_consistency(session)
    assert not report.ok
    assert any(error.code == "stream_gap" for error in report.errors)
    assert any(error.code == "missing_outbox" for error in report.errors)


async def test_experiment_projection_rebuild_matches_authoritative_row(populated_ledger, session) -> None:
    rebuilt = await rebuild_experiment_projection(session, EXPERIMENT_ID)
    stored = await load_experiment_projection(session, EXPERIMENT_ID)
    assert rebuilt.normalized() == stored.normalized()
```

- [ ] **Step 2: Implement ledger consistency checks**

For every stream, verify versions equal `range(1, current_version + 1)`, event aggregate identity matches stream identity, every event has exactly one outbox row, outbox event payload IDs/positions match, and decision aggregate event counts match its projections. Rebuild experiment lifecycle status/timestamps/failure from events and compare with projection without mutating it.

- [ ] **Step 3: Add PostgreSQL integration CI job**

Add a `postgres-integration` job with PostgreSQL 16 service database `maais_test`, `MAAIS_TEST_DATABASE_URL`, `uv sync --locked --dev`, `alembic upgrade head`, all `tests/integration`, and final `alembic_version` assertion `0006`. Keep the Phase 0 migration smoke on `maais` or update its expected head to `0006`.

- [ ] **Step 4: Run the full current-state gate**

Run:

```text
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pyright
.venv/bin/pip-audit --cache-dir /private/tmp/maais-pip-audit-cache
.venv/bin/detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
.venv/bin/pytest -q
MAAIS_TEST_DATABASE_URL=postgresql+psycopg://maais:maais@localhost:5432/maais_test .venv/bin/pytest tests/integration -q
git diff --check
```

Expected: all exit 0, no vulnerabilities, no secrets, zero type errors, all unit/integration tests pass.

- [ ] **Step 5: Record ignored evidence and review Phase 1 definition of done**

Record UTC times, commit/base/worktree identity, exact commands/counts, schema revision, PostgreSQL version, concurrency results, event/projection/outbox reconciliation, and current uncommitted status in `artifacts/readiness/phase-1-verification.json`. Phase 1 completion authorizes the Phase 2 broker plan only; it does not declare the system ready for a timed paper run.

---

## Execution Record

Phase 1 passed locally at `2026-08-02T12:56:09Z`. Both PostgreSQL databases are at revision `0006`; the complete non-database suite reported 412 passed and 18 integration tests skipped without the explicit test URL, while the isolated PostgreSQL suite reported 18 passed. Ruff, Pyright, detect-secrets, pip-audit, Alembic head verification, replay reconciliation, and `git diff --check` all passed. Evidence is recorded in the ignored `artifacts/readiness/phase-1-verification.json` file.

The worktree remains intentionally uncommitted and is not an official soak candidate. Phase 1 completion permits Phase 2 implementation only; it does not make MAAIS ready for a timed paper-trading run.
