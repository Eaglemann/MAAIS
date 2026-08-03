# Paper-week operations

## Start

### Disposable recovery drills

Before the clean soak, run the automated disposable drill once from the exact
candidate commit:

```bash
MAAIS_DOCKER_CONTEXT=desktop-linux scripts/run-process-drills.sh \
  artifacts/manifests/process-drill-YYYY-MM-DD.json \
  artifacts/restore-drills/<drill>/restore-verification.json \
  artifacts/qualification/<qualification-bundle>
```

The runner starts a purpose-bound disposable candidate, captures authoritative
baselines, sends `SIGKILL` only to the PIDs recorded for Mission Control and the
worker, uses the audited recovery path, and proves worker independence,
checkpoint progress, PID replacement, higher worker lease epoch, non-regressing
projections, and passing ledgers. It freezes every raw JSON record and report in
a SHA-256 bundle under `artifacts/process-drills/`, then stops the disposable
run. A failure is evidence to investigate and cannot be used by the soak.

### 24-hour soak

Start the clean soak with the process-drill bundle from the same commit:

```bash
MAAIS_DOCKER_CONTEXT=desktop-linux scripts/start-paper-soak.sh \
  artifacts/manifests/soak-candidate-YYYY-MM-DD.json \
  artifacts/restore-drills/<drill>/restore-verification.json \
  artifacts/qualification/<qualification-bundle> \
  artifacts/process-drills/<process-drill-bundle>
```

The run state is explicitly marked `soak`. The final soak verdict re-hashes the
process-drill bundle and rejects a missing, failed, tampered, or different-commit
bundle. Do not use `start-paper-week.sh` for this gate.

### Seven-day experiment

Use the exact candidate manifest, passing restore-verification artifact, and the
fresh qualification bundle printed by `maais qualify-candidate`:

```bash
MAAIS_DOCKER_CONTEXT=desktop-linux scripts/start-paper-week.sh \
  artifacts/manifests/week-candidate-YYYY-MM-DD.json \
  artifacts/restore-drills/<drill>/restore-verification.json \
  artifacts/qualification/<qualification-bundle>
```

Use the context that owns the PostgreSQL port on this machine; `desktop-linux`
is the Docker Desktop context on macOS, while other installations may use
`default` or another explicit name. Startup, status, daily close, and recovery
compare the container cluster, configured database endpoint, and recorded
candidate system identifier. They fail closed on context drift or cluster
replacement.

The script starts PostgreSQL, applies migrations, verifies the exact-commit
qualification and restore evidence in fail-closed preflight, starts Mission
Control on localhost, starts the paper worker, waits for a running checkpoint
plus active lease, starts a tracked OS sleep inhibitor, and starts the automatic
Berlin-day close supervisor. The four processes run in named detached `tmux`
sessions so they survive the launching terminal. Session names, process IDs,
all three immutable inputs, preflight output, and logs are recorded under
`artifacts/run-state/`. Startup fails if `tmux` or both supported sleep
inhibitors are unavailable.

Mission Control is available at [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Status

```bash
scripts/status-paper-week.sh
uv run maais verify-ledger
uv run maais health --experiment EXPERIMENT_ID --maximum-lag-seconds 180
```

At the end of the separate 24-hour candidate, use the immutable `soak-verdict` command in
`docs/runbooks/seven-day-experiment.md`. A dashboard screenshot or a green point-in-time
health response is not a substitute for that verdict.

Add `--alert` to the health command to emit a structured critical alert and, if
`TELEGRAM_BOT_TOKEN` plus `TELEGRAM_CHAT_ID` are configured, send it to the
sole operator. Notifications are supplementary; Mission Control and persisted
incidents remain authoritative.

Review Mission Control at least twice daily. Confirm:

- worker checkpoint is `running` and lease is `active`;
- checkpoint time advances at least once per minute while the 10-second lease heartbeat remains fresh;
- expected cursor count equals configured symbols and no cursor is halted;
- no unresolved operator-review incident exists;
- kill switch is inactive;
- decision counts, proposal/order/fill counts, account values, and source timestamps are plausible;
- warm-up `insufficient_history` is distinguished from real quality failures.

Use Mission Control's Trade Ledger for every directional proposal. It keeps official
orders/fills, quantity, fees, modeled slippage, and research-only counterfactual outcomes
visibly separate, and opens the exact linked decision bundle for the complete inputs,
agent explanations, ordered gates, hashes, and event timeline. It intentionally does not
invent per-proposal P&L when multiple entries share one net position; authoritative
account and position P&L remain in the account projections and daily reports.

For the Bybit secondary reference, Mission Control preserves the REST snapshot
publication time (`ts`), matching-engine time (`cts`), and local observation
time separately. The publication-age gate is five seconds: the documented
three-second idle level-1 snapshot cadence plus two seconds of transport
allowance. A value beyond five seconds still fails closed and opens an incident.

If an operator-review incident appears, follow `docs/runbooks/incidents.md`. Do not edit rows directly; the supported acknowledge/resolve commands preserve actor, time, rationale, version, domain event, and content hash.

## Daily close

The supervised run closes each completed Berlin calendar day automatically. The following
command remains the supported idempotent manual recovery path:

```bash
scripts/daily-paper-ops.sh EXPERIMENT_ID YYYY-MM-DD
```

This verifies the ledger, writes immutable Markdown/JSON/CSV/Parquet report artifacts with hashes, creates a validated PostgreSQL backup, and checks the running processes/API. The command takes a candidate-and-date operation lock. A retry after a crash reuses the unique verified complete bundle and reconciles it into run state; a concurrent close, ambiguous duplicate, changed bundle identity, or tampered artifact fails closed. Never overwrite or delete a daily bundle during the experiment.

The normal daily command fails closed while the requested Berlin day is still in
progress. `--allow-partial` exists only for the supported stop workflow, which labels the
bundle partial and keeps it out of the seven-day final aggregate.

## Stop

```bash
scripts/stop-paper-week.sh
```

The stop script sends `SIGINT` to allow checkpoint and lease release, refuses to force-kill a stuck worker, verifies the ledger, writes a final partial-day report, creates a backup, and preserves stopped-run state.
