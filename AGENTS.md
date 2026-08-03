# MAAIS Agent Guide

## Purpose and safety boundary

MAAIS is a local, auditable research and paper-trading workstation for USDT
perpetual markets. The supported runtime modes are `replay`, `paper_live`, and
`testnet_smoke`. There is no live-money mode and no production-order adapter.

- Official paper runs use public market data and the local paper broker only.
- Leave `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET` empty for paper runs.
- Never request Docker registry credentials, a Docker account, or exchange keys
  for the paper workflow.
- Demo/Testnet smoke tests are optional protocol checks and never contribute to
  official paper P&L.
- The user is the sole operator. Do not start the seven-day run, merge, deploy,
  configure external alerts, or perform account actions without their explicit
  instruction.

## Current sources of truth

Use these in order:

1. This file for agent constraints and commands.
2. `README.md` and `docs/runbooks/` for current operator workflow.
3. The immutable manifest, qualification, process-drill, restore, preflight,
   soak-verdict, daily-report, and final-report artifacts for run evidence.
4. `docs/superpowers/specs/2026-08-02-maais-paper-trading-observability-design.md`
   for the design baseline.

`ORCHESTRATOR.md`, `PLAN.md`, `ROADMAP.md`, `BEHAVIOURS.md`, `SHIPPED.md`, and
dated delivery plans preserve historical requirements and implementation
decisions. They are not current runtime instructions or readiness evidence.
Any historical live-money text is superseded by the paper-only boundary above.

## Candidate integrity

Official process drills, the 24-hour soak, and the seven-day test are tied to one
clean Git commit, lockfile, schema revision, agent hashes, frozen manifest, and
PostgreSQL cluster identity.

- Do not modify tracked or untracked repository files during an official timed
  run. Stop and invalidate the run first if a code or documentation fix is needed.
- Keep `MAAIS_DOCKER_CONTEXT` explicit. This workspace currently uses
  `desktop-linux`; verify it rather than silently switching engines.
- Never reset or delete the PostgreSQL volume. Back up first and restore only to
  a new suffix-constrained database.
- A green CI run is necessary but does not replace qualification, restore,
  process-drill, soak, or seven-day preflight evidence.
- Do not change thresholds merely to produce trades. Zero fills is an observation;
  complete decisions and rejection rationales remain required.

## Stack and topology

- Python 3.12, `uv`, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16.
- React, TypeScript, Vite, and Vitest under `dashboard/`.
- Docker Compose manages PostgreSQL only.
- `tmux` supervises the paper worker, Mission Control, daily-close supervisor,
  and sleep inhibitor. The worker alone mutates official trading state.
- PostgreSQL is authoritative for experiments, decisions, execution, controls,
  incidents, and event/projection consistency. CSV/Parquet/Markdown/JSON reports
  are immutable, hash-verified exports.

## Essential commands

```bash
export MAAIS_DOCKER_CONTEXT=desktop-linux
docker --context "${MAAIS_DOCKER_CONTEXT}" compose up -d --wait postgres
uv run alembic upgrade head
uv run maais database-identity
uv run maais verify-ledger
```

Backend and security verification:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pip-audit
uv run detect-secrets scan --baseline .secrets.baseline --exclude-files '(^uv\.lock$|^\.superpowers/)'
uv run pytest -q
```

Frontend verification:

```bash
npm --prefix dashboard ci
npm --prefix dashboard audit --audit-level=high
npm --prefix dashboard test
npm --prefix dashboard run typecheck
npm --prefix dashboard run build
```

Use the purpose-bound launchers and runbooks instead of assembling long-running
commands manually:

- `scripts/start-paper-drill.sh`
- `scripts/run-process-drills.sh`
- `scripts/start-paper-soak.sh`
- `scripts/start-paper-week.sh`
- `scripts/status-paper-week.sh`
- `scripts/stop-paper-week.sh`
- `scripts/recover-paper-week.sh`

## Change discipline

- Use tests first for behavior changes and reproduce defects before fixing them.
- Preserve unrelated user changes and ignored evidence.
- Keep commits small, explanatory, and free of co-author trailers.
- Push incremental commits to the current feature branch after local verification.
- Before claiming readiness, inspect the current runtime and every required
  immutable gate; never infer completion from plans, intentions, or old artifacts.
