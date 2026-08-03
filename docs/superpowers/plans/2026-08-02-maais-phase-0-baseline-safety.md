# MAAIS Phase 0 Baseline Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a reproducible local baseline that cannot construct authenticated production execution, fails closed on missing trade approvals, starts PostgreSQL reliably, and enforces current quality/safety gates.

**Architecture:** Keep public Binance market-data adapters separate from an isolated authenticated Demo/Testnet adapter. Introduce an explicit three-value run mode at configuration ingress, harden the legacy execution engine as a temporary Testnet-smoke boundary, and make PostgreSQL plus CI reproducible before adding the event ledger. This phase does not implement the paper broker or claim experiment readiness.

**Tech Stack:** Python 3.12, Pydantic Settings, asyncio, SQLAlchemy 2, Alembic, PostgreSQL 16, Docker Compose, pytest, Ruff, Pyright, pip-audit, detect-secrets.

**Execution status (2026-08-02):** Phase 0 is locally verified. Current ignored evidence is recorded at `artifacts/readiness/phase-0-verification.json`. This status proves only the baseline gate; it does not declare the paper platform ready.

## Global Constraints

- Supported modes are exactly `REPLAY`, `PAPER_LIVE`, and `TESTNET_SMOKE`; there is no `LIVE` mode.
- Public live market data requires no credential.
- Authenticated Binance access is Demo/Testnet only and uses `https://demo-fapi.binance.com`.
- Production authenticated execution endpoints must be absent from `maais/execution/` and from every client factory.
- The legacy `ExecutionEngine` is Testnet-smoke infrastructure only; the Phase 2 local paper broker will replace it for official P&L.
- Trade submission is fail-closed when decision approval, risk approval, monitoring approval, symbol agreement, or approved notional validation fails.
- A filled order with failed compliance persistence remains truthfully `FILLED` but has `approved=False`, `compliance_recorded=False`, and a non-empty error.
- PostgreSQL 16 is the local authoritative database baseline.
- All money and notional comparisons use `Decimal`.
- No user credential or `.env` value is committed.
- Do not commit, push, deploy, enable credentials, or start a timed experiment without explicit user authorization.

---

## File Structure

### Create

- `.env.template` - non-secret local configuration contract using Demo/Testnet credential names only.
- `compose.yaml` - PostgreSQL 16 local service with a persistent volume and health check.
- `maais/config/modes.py` - closed `RunMode` enum and mode capability helpers.
- `maais/execution/protocols.py` - structural protocol used by the engine, fill tracker, and funding tracker.
- `maais/execution/testnet/__init__.py` - explicit Demo/Testnet adapter package.
- `maais/execution/testnet/binance_client.py` - signed client pinned to the Binance Demo Futures base URL.
- `tests/test_execution_safety.py` - source/factory safety and fail-closed engine tests.
- `tests/test_settings.py` - mode and credential-boundary tests.
- `.github/workflows/ci.yml` - backend, database, type, dependency, secret, and safety gates.

### Modify

- `.gitignore` - local artifacts, backups, data, and visual-companion scratch exclusions.
- `pyproject.toml` and `uv.lock` - quality/security dependencies and tool configuration.
- `maais/config/settings.py` - typed run mode, Demo/Testnet credential names, explicit database configuration.
- `maais/execution/binance_client.py` - compatibility import only; production URL and signed implementation removed.
- `maais/execution/engine.py` - approval/notional checks and truthful compliance outcome.
- `maais/execution/fill_tracker.py` - depend on `AuthenticatedExecutionClient` protocol.
- `maais/execution/funding_tracker.py` - depend on `AuthenticatedExecutionClient` protocol.
- `maais/execution/schemas.py` - add `compliance_recorded` to `OrderResult`.
- `tests/test_execution.py` - pass explicit monitoring/reference inputs and assert compliance state.
- `README.md`, `PLAN.md`, `SHIPPED.md`, `ORCHESTRATOR.md` - replace stale readiness language with current commands and gates.

## Interfaces Fixed by This Plan

```python
class RunMode(str, Enum):
    REPLAY = "replay"
    PAPER_LIVE = "paper_live"
    TESTNET_SMOKE = "testnet_smoke"

    @property
    def permits_authenticated_exchange(self) -> bool:
        return self is RunMode.TESTNET_SMOKE


class AuthenticatedExecutionClient(Protocol):
    async def set_leverage(self, symbol: str, leverage: int) -> int: ...
    async def place_order(self, request: OrderRequest) -> dict[str, object]: ...
    async def get_order(self, symbol: str, order_id: str) -> dict[str, object]: ...
    async def get_funding_payments(self, symbol: str, limit: int = 50) -> list[dict[str, object]]: ...


class BinanceDemoFuturesClient:
    def __init__(self, api_key: str, api_secret: str) -> None: ...


def build_authenticated_execution_client(settings: Settings) -> BinanceDemoFuturesClient:
    """Raise ValueError unless settings.run_mode is TESTNET_SMOKE and both Demo credentials exist."""


async def ExecutionEngine.execute(
    request: OrderRequest,
    position_size: PositionSize,
    decision: DecisionResult,
    *,
    monitoring_approved: bool,
    reference_price: Decimal,
    is_closing: bool = False,
) -> OrderResult: ...
```

---

### Task 1: Repository Hygiene and Truthful Status

**Files:**
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `PLAN.md`
- Modify: `SHIPPED.md`
- Modify: `ORCHESTRATOR.md`
- Test: `tests/test_repository_contract.py`

**Interfaces:**
- Consumes: the design readiness gates and the existing `uv`/Alembic commands.
- Produces: documented local commands and a source-controlled assertion that generated state is ignored.

- [ ] **Step 1: Write the failing repository-contract tests**

```python
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_generated_runtime_state_is_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text()
    for entry in (".superpowers/", "artifacts/", "backups/", "data/"):
        assert entry in ignored


def test_readme_does_not_claim_paper_readiness() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    assert "not yet ready for the seven-day paper experiment" in readme
```

- [ ] **Step 2: Run the focused tests and observe the expected failure**

Run: `.venv/bin/pytest tests/test_repository_contract.py -q`

Expected: FAIL because required ignore entries and truthful readiness sentence are absent.

- [ ] **Step 3: Add exact ignore entries and replace stale status claims**

Append these entries to `.gitignore`:

```gitignore
# MAAIS local runtime and design-session state
.superpowers/
artifacts/
backups/
data/
```

Make `README.md` begin with a current-state warning, the exact setup/test/migration commands, the three supported modes, and a link to the design/master plan. Mark legacy batches in `PLAN.md` and `SHIPPED.md` as implemented components rather than experiment-readiness evidence. Set the next action in `ORCHESTRATOR.md` to Phase 0 baseline safety.

- [ ] **Step 4: Run repository-contract tests**

Run: `.venv/bin/pytest tests/test_repository_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Review checkpoint without committing**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intentional files are changed. Do not commit without explicit authorization.

### Task 2: Explicit Runtime Modes and Configuration Boundary

**Files:**
- Create: `maais/config/modes.py`
- Create: `.env.template`
- Modify: `maais/config/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: Pydantic Settings and environment variables prefixed by exact field names.
- Produces: `RunMode`, `Settings.run_mode`, `Settings.binance_demo_api_key`, and `Settings.binance_demo_api_secret`.

- [ ] **Step 1: Write failing mode/settings tests**

```python
import pytest
from pydantic import ValidationError

from maais.config.modes import RunMode
from maais.config.settings import Settings


def test_run_modes_are_closed() -> None:
    assert {mode.value for mode in RunMode} == {"replay", "paper_live", "testnet_smoke"}
    assert RunMode.TESTNET_SMOKE.permits_authenticated_exchange
    assert not RunMode.PAPER_LIVE.permits_authenticated_exchange


def test_settings_default_to_replay_without_credentials() -> None:
    settings = Settings(_env_file=None)
    assert settings.run_mode is RunMode.REPLAY
    assert settings.binance_demo_api_key == ""
    assert settings.binance_demo_api_secret == ""


def test_live_is_not_a_valid_mode() -> None:
    with pytest.raises(ValidationError):
        Settings(run_mode="live", _env_file=None)
```

- [ ] **Step 2: Run the focused tests and observe import failure**

Run: `.venv/bin/pytest tests/test_settings.py -q`

Expected: FAIL because `maais.config.modes` does not exist.

- [ ] **Step 3: Implement the closed mode enum and typed settings**

```python
# maais/config/modes.py
from enum import Enum


class RunMode(str, Enum):
    REPLAY = "replay"
    PAPER_LIVE = "paper_live"
    TESTNET_SMOKE = "testnet_smoke"

    @property
    def permits_authenticated_exchange(self) -> bool:
        return self is RunMode.TESTNET_SMOKE
```

Replace generic authenticated credential fields in `Settings` with:

```python
run_mode: RunMode = RunMode.REPLAY
binance_demo_api_key: str = ""
binance_demo_api_secret: str = ""
database_url: str = "postgresql+psycopg://maais:maais@localhost:5432/maais"
```

Create `.env.template` containing only non-secret defaults and empty Demo/Testnet credential values.

- [ ] **Step 4: Run focused and existing configuration tests**

Run: `.venv/bin/pytest tests/test_settings.py tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Review checkpoint without committing**

Run: `git diff --check && git diff -- maais/config .env.template tests/test_settings.py`

Expected: no generic production credential fields and no secret values.

### Task 3: Isolate Authenticated Execution to Binance Demo/Testnet

**Files:**
- Create: `maais/execution/protocols.py`
- Create: `maais/execution/testnet/__init__.py`
- Create: `maais/execution/testnet/binance_client.py`
- Modify: `maais/execution/binance_client.py`
- Modify: `maais/execution/engine.py`
- Modify: `maais/execution/fill_tracker.py`
- Modify: `maais/execution/funding_tracker.py`
- Test: `tests/test_execution_safety.py`

**Interfaces:**
- Consumes: `RunMode`, `Settings`, `OrderRequest`.
- Produces: `AuthenticatedExecutionClient`, `BinanceDemoFuturesClient`, and `build_authenticated_execution_client(settings)`.

- [ ] **Step 1: Write failing source and factory safety tests**

```python
from pathlib import Path

import pytest

from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.execution.testnet.binance_client import (
    DEMO_FUTURES_BASE_URL,
    BinanceDemoFuturesClient,
    build_authenticated_execution_client,
)


def test_authenticated_execution_source_contains_no_production_url() -> None:
    source = "\n".join(
        path.read_text()
        for path in Path("maais/execution").rglob("*.py")
    )
    assert "https://fapi.binance.com" not in source


def test_demo_client_is_pinned_to_demo_url() -> None:
    assert DEMO_FUTURES_BASE_URL == "https://demo-fapi.binance.com"


def test_factory_rejects_non_testnet_modes() -> None:
    settings = Settings(run_mode=RunMode.PAPER_LIVE, _env_file=None)
    with pytest.raises(ValueError, match="testnet_smoke"):
        build_authenticated_execution_client(settings)


def test_factory_requires_both_demo_credentials() -> None:
    settings = Settings(run_mode=RunMode.TESTNET_SMOKE, _env_file=None)
    with pytest.raises(ValueError, match="Demo credentials"):
        build_authenticated_execution_client(settings)


def test_factory_constructs_only_demo_client() -> None:
    settings = Settings(
        run_mode=RunMode.TESTNET_SMOKE,
        binance_demo_api_key="key",
        binance_demo_api_secret="secret",
        _env_file=None,
    )
    assert isinstance(build_authenticated_execution_client(settings), BinanceDemoFuturesClient)
```

- [ ] **Step 2: Run safety tests and observe import/source failures**

Run: `.venv/bin/pytest tests/test_execution_safety.py -q`

Expected: FAIL because the Testnet package is absent and the production URL remains.

- [ ] **Step 3: Add the structural client protocol**

```python
from typing import Protocol

from maais.execution.schemas import OrderRequest


class AuthenticatedExecutionClient(Protocol):
    async def set_leverage(self, symbol: str, leverage: int) -> int: ...
    async def place_order(self, request: OrderRequest) -> dict[str, object]: ...
    async def get_order(self, symbol: str, order_id: str) -> dict[str, object]: ...
    async def get_funding_payments(
        self, symbol: str, limit: int = 50
    ) -> list[dict[str, object]]: ...
```

- [ ] **Step 4: Move the signed adapter behind the Demo factory**

Set `DEMO_FUTURES_BASE_URL = "https://demo-fapi.binance.com"`; make credentials explicit constructor arguments; preserve order/query/cancel/funding behavior; and implement:

```python
def build_authenticated_execution_client(settings: Settings) -> BinanceDemoFuturesClient:
    if settings.run_mode is not RunMode.TESTNET_SMOKE:
        raise ValueError("authenticated execution is limited to testnet_smoke")
    if not settings.binance_demo_api_key or not settings.binance_demo_api_secret:
        raise ValueError("both Binance Demo credentials are required")
    return BinanceDemoFuturesClient(
        api_key=settings.binance_demo_api_key,
        api_secret=settings.binance_demo_api_secret,
    )
```

Make `maais/execution/binance_client.py` a compatibility re-export of `BinanceDemoFuturesClient` under the old name, with no URL or implementation. Change engine/trackers to the protocol type so no factory is bypassed.

- [ ] **Step 5: Run focused and legacy execution tests**

Run: `.venv/bin/pytest tests/test_execution_safety.py tests/test_execution.py -q`

Expected: source/factory tests PASS; any engine signature failures are intentionally resolved in Task 4.

- [ ] **Step 6: Review checkpoint without committing**

Run: `rg -n "fapi\.binance\.com|binance_api_key|binance_api_secret|RunMode\.LIVE|class Live" maais tests .env.template`

Expected: no output except explicitly asserted forbidden strings inside `tests/test_execution_safety.py`.

### Task 4: Fail-Closed Legacy Execution Authorization

**Files:**
- Modify: `maais/execution/schemas.py`
- Modify: `maais/execution/engine.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_execution_safety.py`

**Interfaces:**
- Consumes: approved `DecisionResult`, approved `PositionSize`, monitoring boolean, positive reference price.
- Produces: an `OrderResult` with truthful venue status, approval, error, and `compliance_recorded` state.

- [ ] **Step 1: Add failing engine safety tests**

```python
@pytest.mark.parametrize(
    ("decision_approved", "risk_approved", "monitoring_approved", "error"),
    [
        (False, True, True, "decision_not_approved"),
        (True, False, True, "risk_not_approved"),
        (True, True, False, "monitoring_not_approved"),
    ],
)
async def test_execution_fails_closed_before_client_call(
    decision_approved: bool,
    risk_approved: bool,
    monitoring_approved: bool,
    error: str,
) -> None:
    engine, client, _writer = build_engine()
    decision = decision_fixture(approved=decision_approved)
    size = position_size_fixture(approved=risk_approved)
    result = await engine.execute(
        order_fixture(),
        size,
        decision,
        monitoring_approved=monitoring_approved,
        reference_price=Decimal("50000"),
    )
    assert result.status is OrderStatus.REJECTED
    assert result.error_message == error
    client.place_order.assert_not_awaited()


async def test_execution_rejects_notional_above_approved_size() -> None:
    engine, client, _writer = build_engine()
    result = await engine.execute(
        order_fixture(quantity=Decimal("1")),
        position_size_fixture(final_usd=2000.0),
        decision_fixture(),
        monitoring_approved=True,
        reference_price=Decimal("50000"),
    )
    assert result.error_message == "approved_notional_exceeded"
    client.place_order.assert_not_awaited()


async def test_filled_order_reports_compliance_persistence_failure() -> None:
    engine, _client, writer = build_engine()
    writer.record_open.side_effect = RuntimeError("database unavailable")
    result = await engine.execute(
        order_fixture(),
        position_size_fixture(),
        decision_fixture(),
        monitoring_approved=True,
        reference_price=Decimal("50000"),
    )
    assert result.status is OrderStatus.FILLED
    assert not result.approved
    assert not result.compliance_recorded
    assert result.error_message == "post_fill_recording_failed: database unavailable"
```

- [ ] **Step 2: Run engine safety tests and observe failures**

Run: `.venv/bin/pytest tests/test_execution_safety.py -k 'fails_closed or notional or compliance' -q`

Expected: FAIL because approvals are not checked and `compliance_recorded` does not exist.

- [ ] **Step 3: Add truthful result state and centralized preflight validation**

Add `compliance_recorded: bool` to `OrderResult`. In `ExecutionEngine`, add a `_rejected(...)` helper and validate, before leverage or client calls:

```python
if not decision.approved:
    return self._rejected(request, estimated_cost, "decision_not_approved")
if not position_size.approved:
    return self._rejected(request, estimated_cost, "risk_not_approved")
if not monitoring_approved:
    return self._rejected(request, estimated_cost, "monitoring_not_approved")
if request.symbol != decision.symbol or request.symbol != position_size.symbol:
    return self._rejected(request, estimated_cost, "symbol_mismatch")
if reference_price <= 0:
    return self._rejected(request, estimated_cost, "invalid_reference_price")
validation_price = request.price if request.order_type is OrderType.LIMIT else reference_price
requested_notional = request.quantity * validation_price
approved_notional = Decimal(str(position_size.final_usd))
if not request.reduce_only and requested_notional > approved_notional:
    return self._rejected(request, estimated_cost, "approved_notional_exceeded")
```

All pre-fill failure results set `compliance_recorded=False`. Successful non-filled terminal results set it `True` because no fill record is required. Successful filled results set it only after the writer returns. A writer exception preserves `FILLED` and the fill, but sets `approved=False` and the exact prefixed error.

- [ ] **Step 4: Update all legacy execution calls and assertions**

Pass `monitoring_approved=True` and `reference_price=Decimal("50000")` in legacy happy-path tests. Make the existing request quantity consistent with `final_usd` and assert `result.compliance_recorded` after a successful fill.

- [ ] **Step 5: Run focused and full tests**

Run: `.venv/bin/pytest tests/test_execution.py tests/test_execution_safety.py -q`

Expected: PASS.

Run: `.venv/bin/pytest -q`

Expected: all tests PASS.

- [ ] **Step 6: Review checkpoint without committing**

Run: `git diff --check && git diff -- maais/execution tests/test_execution.py tests/test_execution_safety.py`

Expected: every return path initializes `compliance_recorded`; all rejects occur before client calls.

### Task 5: PostgreSQL Compose and Migration Smoke

**Files:**
- Create: `compose.yaml`
- Modify: `.env.template`
- Modify: `README.md`
- Test: existing Alembic migrations `alembic/versions/0001_baseline.py` through `0004_agent_weights.py`

**Interfaces:**
- Consumes: `Settings.database_url`, Alembic `get_url()`, port 5432.
- Produces: healthy service `postgres`, database/user/password `maais`, persistent volume `maais_postgres_data`.

- [ ] **Step 1: Add the exact Compose service**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: maais
      POSTGRES_USER: maais
      POSTGRES_PASSWORD: maais
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - maais_postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U maais -d maais"]
      interval: 2s
      timeout: 3s
      retries: 30

volumes:
  maais_postgres_data:
```

- [ ] **Step 2: Validate the Compose model**

Run: `docker compose config --quiet`

Expected: exit 0.

- [ ] **Step 3: Start PostgreSQL and wait for health**

Run: `docker compose up -d --wait postgres`

Expected: service `postgres` is healthy.

- [ ] **Step 4: Upgrade a fresh database to Alembic head**

Run: `.venv/bin/alembic upgrade head`

Expected: upgrades 0001 through 0004 without error.

- [ ] **Step 5: Verify schema version and restart persistence**

Run: `docker compose exec -T postgres psql -U maais -d maais -Atc 'select version_num from alembic_version'`

Expected: `0004`.

Run: `docker compose restart postgres && docker compose up -d --wait postgres`

Expected: healthy service; the same query still returns `0004`.

- [ ] **Step 6: Review checkpoint without removing data**

Run: `docker compose ps && git diff --check`

Expected: PostgreSQL healthy. Preserve the volume; do not run destructive reset commands.

### Task 6: Quality, Type, Dependency, and Secret Gates

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: Python source/tests selected by Ruff/Pyright output
- Create: `.secrets.baseline`

**Interfaces:**
- Consumes: all Python 3.12 source and test files.
- Produces: deterministic developer commands for format, lint, type, dependency, and secret checks.

- [ ] **Step 1: Add development tools and scoped configuration**

Add to the `dev` group:

```toml
"detect-secrets>=1.5",
"hypothesis>=6.138",
"pip-audit>=2.9",
"pyright>=1.1.403",
"pytest-cov>=6.2",
```

Add:

```toml
[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "basic"
include = ["maais"]
exclude = [".venv", "data", "artifacts", "backups"]
reportMissingTypeStubs = false
```

Run: `uv lock && uv sync --dev`

Expected: lock and environment update successfully.

- [ ] **Step 2: Capture current failures**

Run: `.venv/bin/ruff format --check .`

Expected before cleanup: FAIL if formatting drift exists.

Run: `.venv/bin/ruff check .`

Expected before cleanup: current lint failures are listed.

Run: `.venv/bin/pyright`

Expected before cleanup: current type failures are listed.

- [ ] **Step 3: Apply deterministic formatting and fix diagnostics at source**

Run: `.venv/bin/ruff format .`

Then fix each Ruff and Pyright diagnostic without blanket `noqa`, file-wide ignores, or `Any` casts that hide an execution/persistence boundary. Use explicit collection types and protocol types introduced in Task 3.

- [ ] **Step 4: Create and verify a secret baseline**

Run: `.venv/bin/detect-secrets scan --exclude-files '(^uv\.lock$|^\.superpowers/)' > .secrets.baseline`

Run: `.venv/bin/detect-secrets audit .secrets.baseline`

Expected: no verified secrets; generated baseline contains only reviewed false positives.

- [ ] **Step 5: Run all local quality gates**

Run: `.venv/bin/ruff format --check .`

Run: `.venv/bin/ruff check .`

Run: `.venv/bin/pyright`

Run: `.venv/bin/pip-audit`

Run: `.venv/bin/detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'`

Run: `.venv/bin/pytest -q`

Expected: every command exits 0.

- [ ] **Step 6: Review checkpoint without committing**

Run: `git diff --stat && git diff --check`

Expected: formatting changes are mechanical; safety behavior changes remain limited to Tasks 2-4.

### Task 7: Continuous Integration with PostgreSQL and Safety Gates

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `uv.lock`, Compose-equivalent PostgreSQL 16 service, local quality commands.
- Produces: CI jobs `quality`, `test`, `security`, and `migration` on pushes and pull requests.

- [ ] **Step 1: Create the workflow with exact gates**

```yaml
name: CI

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv sync --locked --dev
      - run: uv run ruff format --check .
      - run: uv run ruff check .
      - run: uv run pyright

  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked --dev
      - run: uv run pytest -q

  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked --dev
      - run: uv run pip-audit
      - run: uv run detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
      - run: uv run pytest tests/test_execution_safety.py -q

  migration:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: maais
          POSTGRES_USER: maais
          POSTGRES_PASSWORD: maais
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U maais -d maais"
          --health-interval 2s
          --health-timeout 3s
          --health-retries 30
    env:
      DATABASE_URL: postgresql+psycopg://maais:maais@localhost:5432/maais
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync --locked --dev
      - run: uv run alembic upgrade head
      - run: PGPASSWORD=maais psql -h localhost -U maais -d maais -Atc 'select version_num from alembic_version' | grep -Fx 0004
```

- [ ] **Step 2: Validate workflow syntax and command parity**

Run: `rg -n "ruff format --check|ruff check|pyright|pip-audit|detect-secrets|pytest|alembic upgrade head|test_execution_safety" .github/workflows/ci.yml README.md`

Expected: every gate appears in CI and the README command block.

- [ ] **Step 3: Run the CI commands locally against the healthy database**

Run: `.venv/bin/ruff format --check . && .venv/bin/ruff check . && .venv/bin/pyright && .venv/bin/pip-audit && .venv/bin/pytest -q && .venv/bin/alembic upgrade head`

Expected: exit 0.

- [ ] **Step 4: Review checkpoint without committing**

Run: `git diff --check && git status --short`

Expected: workflow and documentation are present; no generated `.env`, database data, or credentials are tracked.

### Task 8: Phase 0 Verification and Evidence Record

**Files:**
- Create: `artifacts/readiness/phase-0-verification.json` (generated, ignored)
- Modify: `docs/superpowers/plans/2026-08-02-maais-phase-0-baseline-safety.md` (checkbox evidence only)

**Interfaces:**
- Consumes: all Phase 0 commands and the current Git state.
- Produces: current local evidence; does not declare paper-platform or seven-day readiness.

- [ ] **Step 1: Run the complete clean-room verification sequence**

Run each command independently and record exit code, UTC time, and concise output in the ignored evidence JSON:

```text
docker compose config --quiet
docker compose up -d --wait postgres
.venv/bin/alembic upgrade head
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pyright
.venv/bin/pip-audit
.venv/bin/detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
.venv/bin/pytest -q
git diff --check
```

- [ ] **Step 2: Re-run invariant-focused tests separately**

Run: `.venv/bin/pytest tests/test_settings.py tests/test_execution_safety.py tests/test_repository_contract.py -q`

Expected: PASS with no network access.

- [ ] **Step 3: Confirm prohibited production construction is absent**

Run: `rg -n "https://fapi\.binance\.com|run_mode.?=.?['\"]live|RunMode\.LIVE" maais .env.template compose.yaml`

Expected: no output.

- [ ] **Step 4: Confirm the database baseline**

Run: `docker compose exec -T postgres psql -U maais -d maais -Atc 'select version_num from alembic_version'`

Expected: `0004`.

- [ ] **Step 5: Perform the phase gate review**

Check each Phase 0 definition-of-done item in the master plan against fresh command output. Record any failure as an open gate; do not relabel the overall system as ready. Phase 0 completion authorizes planning Phase 1 only.

- [ ] **Step 6: Present the uncommitted diff for user-controlled versioning**

Run: `git status --short && git diff --stat && git diff --check`

Expected: all intended source, tests, operations, and design files are visible; no commit or push has occurred.
