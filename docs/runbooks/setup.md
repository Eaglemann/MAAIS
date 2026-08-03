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
npm exec -- playwright-cli install-browser chrome-for-testing
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

The repository uses the local Docker engine only. It does not need a Docker
account, registry token, or credential-helper/keychain access for paper trading.
The PostgreSQL image is public; reject an unexpected credential prompt rather
than entering a personal Docker password.

Keep `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET` empty for paper-live runs. The start script sets `RUN_MODE=paper_live` for preflight and both long-running services. It also starts Mission Control and the worker with `ENVIRONMENT=production`, which enables JSON-line logs and disables SQL echo for the multi-day run.

Create the isolated PostgreSQL test database once. First check whether it exists:

```bash
docker --context "${MAAIS_DOCKER_CONTEXT}" compose exec -T postgres \
  psql -U maais -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'maais_test'"
```

If that prints no `1`, create it:

```bash
docker --context "${MAAIS_DOCKER_CONTEXT}" compose exec -T postgres \
  createdb -U maais maais_test
```

## Candidate preparation

Candidate manifests and qualification evidence must come from a clean committed
worktree. After all code and documentation changes are committed, run the
exact-commit qualification. `MAAIS_TEST_DATABASE_URL` may come from `.env`; it
must use PostgreSQL and a database name ending in `_test`:

```bash
uv run maais qualify-candidate \
  --repository . \
  --output artifacts/qualification
```

The command always runs every required migration, full backend branch-coverage,
golden replay, fault-injection, formatting, lint, type, dependency, secret,
execution-safety, frontend, build, and real-browser gate. It preserves a hashed
log for every gate even when one fails. The resulting immutable bundle is tied
to the clean Git commit, lockfile, migration head, and agent source hashes. It
expires for candidate startup after 24 hours and cannot be reused after any code
or dependency change.

Then prepare the manifest from that unchanged commit:

```bash
uv run maais prepare-paper-live \
  --name week-candidate-YYYY-MM-DD \
  --output artifacts/manifests/week-candidate-YYYY-MM-DD.json
```

Manifest preparation contacts only public market-data endpoints and freezes
exchange filters, symbol mappings, code, lockfile, schema, component, and agent
identities.
