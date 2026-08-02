# MAAIS

MAAIS is a local, auditable multi-agent research and paper-trading platform for
USDT perpetual markets.

> **Current status:** The component prototype is **not yet ready for the seven-day paper experiment**.
> The paper broker, authoritative event ledger,
> live orchestrator, Mission Control dashboard, recovery drills, and 24-hour
> soak gate are being completed under the plans linked below. No live-money
> execution is in scope.

## Safety boundary

The supported runtime modes are exactly:

- `replay` — deterministic historical/research execution;
- `paper_live` — public live market data with the local paper broker;
- `testnet_smoke` — authenticated Binance Demo/Testnet protocol checks only.

There is no live-money mode. Public market-data connectors do not require API
credentials. Authenticated exchange operations are restricted to Binance Demo
Futures and are never used to calculate official paper P&L.

## Local setup

Requirements: Python 3.12, `uv`, Docker Desktop with Compose, and PostgreSQL
client tools.

```bash
cp .env.template .env
uv sync --dev
docker compose up -d --wait postgres
uv run alembic upgrade head
```

## Verification

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pip-audit
uv run detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
uv run pytest -q
```

## Paper-week operator workflow

The operational CLI now includes ledger verification, immutable daily reports,
analysis-ready CSV/Parquet exports, validated backups, suffix-constrained restore
drills, and candidate preflight. The timed run remains blocked until the full
test/fault-injection suite and 24-hour soak pass.

- [Local setup](docs/runbooks/setup.md)
- [Normal operations](docs/runbooks/operations.md)
- [Incident response](docs/runbooks/incidents.md)
- [Recovery and restore](docs/runbooks/recovery.md)
- [Seven-day protocol](docs/runbooks/seven-day-experiment.md)

## Design and delivery

- [Paper-trading and observability design](docs/superpowers/specs/2026-08-02-maais-paper-trading-observability-design.md)
- [Master delivery plan](docs/superpowers/plans/2026-08-02-maais-master-delivery-plan.md)
- [Phase 0 safety plan](docs/superpowers/plans/2026-08-02-maais-phase-0-baseline-safety.md)

Historical batch documents describe implemented components, not current system
readiness. A seven-day test starts only after all preflight gates in the design
have fresh evidence.
