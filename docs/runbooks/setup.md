# Local paper-platform setup

This platform supports public live market data with simulated execution only. It has no live-money mode or authenticated production-order adapter.

## Requirements

- macOS or Linux with Python 3.12, `uv`, Docker Compose, PostgreSQL 16 client tools, Node/npm, `curl`, `jq`, and `tmux`.
- `caffeinate` on macOS or `systemd-inhibit` on Linux so the operator script can block machine sleep for the timed run.
- At least 5 GiB free disk space for the candidate gate; more is preferable for logs and daily backups.
- A machine that remains powered and connected during the timed run.

## One-time setup

```bash
cp .env.template .env
uv sync --dev
npm --prefix dashboard ci
npm --prefix dashboard run build
export MAAIS_DOCKER_CONTEXT=desktop-linux  # use `docker context show` to choose yours
docker --context "${MAAIS_DOCKER_CONTEXT}" compose up -d --wait postgres
uv run alembic upgrade head
uv run maais database-identity
uv run maais verify-ledger
```

Keep the Docker context explicit when Docker Desktop, OrbStack, Colima, or
another engine coexist. Candidate startup reads `MAAIS_DOCKER_CONTEXT` (or the
current context when it is unset), proves that the Compose container and the
configured application endpoint share one PostgreSQL `system_identifier`, and
records both identities in run state. A mismatch fails before migrations,
preflight, or worker startup.

Keep `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET` empty for paper-live runs. The start script sets `RUN_MODE=paper_live` for preflight and both long-running services. It also starts Mission Control and the worker with `ENVIRONMENT=production`, which enables JSON-line logs and disables SQL echo for the multi-day run.

## Candidate preparation

Candidate manifests must be generated from a clean committed worktree. After all code and documentation changes are committed:

```bash
uv run maais prepare-paper-live \
  --name week-candidate-YYYY-MM-DD \
  --output artifacts/manifests/week-candidate-YYYY-MM-DD.json
```

Manifest preparation contacts only public market-data endpoints and freezes exchange filters, symbol mappings, code, lockfile, schema, component, and agent identities.
