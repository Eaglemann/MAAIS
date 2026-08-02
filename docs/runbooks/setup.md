# Local paper-platform setup

This platform supports public live market data with simulated execution only. It has no live-money mode or authenticated production-order adapter.

## Requirements

- macOS or Linux with Python 3.12, `uv`, Docker Compose, PostgreSQL 16 client tools, Node/npm, `curl`, and `jq`.
- `caffeinate` on macOS or `systemd-inhibit` on Linux so the operator script can block machine sleep for the timed run.
- At least 5 GiB free disk space for the candidate gate; more is preferable for logs and daily backups.
- A machine that remains powered and connected during the timed run.

## One-time setup

```bash
cp .env.template .env
uv sync --dev
npm --prefix dashboard ci
npm --prefix dashboard run build
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run maais verify-ledger
```

Keep `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET` empty for paper-live runs. The start script sets `RUN_MODE=paper_live` only for preflight and the worker process.

## Candidate preparation

Candidate manifests must be generated from a clean committed worktree. After all code and documentation changes are committed:

```bash
uv run maais prepare-paper-live \
  --name week-candidate-YYYY-MM-DD \
  --output artifacts/manifests/week-candidate-YYYY-MM-DD.json
```

Manifest preparation contacts only public market-data endpoints and freezes exchange filters, symbol mappings, code, lockfile, schema, component, and agent identities.
