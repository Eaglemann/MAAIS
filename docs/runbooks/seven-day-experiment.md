# Seven-day paper experiment protocol

## Entry gates

The seven-day clock starts only after all of the following have fresh evidence:

- clean committed candidate and immutable manifest;
- a hash-verified qualification bundle no older than 24 hours, tied to the exact
  clean repository identity, containing passing unit, PostgreSQL integration,
  branch-coverage, static, secret, dependency, frontend, and real-browser checks;
- deterministic replay and fault-injection suite;
- validated database backup and passing restore drill;
- 24-hour live-data soak with no unexplained state, duplicates, unhandled exceptions, or unrecovered gaps;
- Mission Control and daily report reconciliation;
- no live-money path or exchange credentials.

The required fault matrix and process-drill boundaries are defined in
`docs/testing/fault-injection.md`. `scripts/run-process-drills.sh` uses a
purpose-bound disposable candidate and must produce a passing exact-commit
bundle before `scripts/start-paper-soak.sh` can begin the clean soak. The soak
readiness verdict independently re-verifies that bundle.

Operator health is evaluated at the PostgreSQL snapshot timestamp, not at the end of a
potentially long ledger scan. The report records `snapshot_at`, `completed_at`, and
`verification_duration_seconds`; its required `verification_freshness` check fails if the
scan exceeds the configured maximum lag. Full ledger verification uses set-based,
query-count-bounded reconciliation so accumulated experiments, decisions, orders, and fills
do not create per-row database queries.

After at least 24 uninterrupted hours, while all four supervised processes are still
running, freeze the soak decision:

```bash
uv run maais soak-verdict \
  --experiment EXPERIMENT_ID \
  --state artifacts/run-state/current.json \
  --repository . \
  --output artifacts/readiness
```

The command exits nonzero unless the candidate identity and preflight match, the full
duration elapsed, every symbol has contiguous one-minute decision cardinality, runtime and
ledger health pass, every decision has its market frame, summary, all eight configured
agent rows with nonempty input/reason/explanation metadata, all 18 quality evaluations, and
contiguous gate sequence, every required data-quality failure was quarantined rather than
admitted, no process restarted, and the worker, dashboard, daily-close supervisor, and
sleep-inhibitor logs contain only structured JSON with no error-level event. Its JSON,
Markdown, and SHA-256 manifest form the readiness verdict; a failed bundle is evidence to
investigate, never permission to begin the week.

The seven-day launcher requires that bundle explicitly. Prepare a new week
experiment manifest from the exact same clean commit; do not reuse or restart
the stopped soak experiment. The launch preflight re-hashes the bundle,
re-validates every ordered soak gate, matches the full repository and agent
identity, and rejects evidence more than 24 hours old.

## During the run

- On a MacBook, keep AC connected, battery reserve above 50%, and the lid open.
  The tracked sleep inhibitor cannot override macOS clamshell sleep. Any host
  sleep invalidates the uninterrupted clock; an always-on desktop host is the
  preferred local topology for the seven-day experiment.
- Do not change code, dependencies, manifest, thresholds, symbols, agent weights, fees, latency, or risk settings.
- Generate one immutable report and backup for each completed Berlin day. The supported
  daily-close command is concurrency-locked and crash-resumable; retry that command rather
  than invoking the report writer separately.
- Review incidents and decision explanations without editing historical records.
- Machine sleep, network outage, worker restart, and API interruption must be logged. Recovery must preserve exactly-once identities.
- A material defect, ledger mismatch, duplicate, missing protection, or configuration/code change ends the candidate. Fix it, commit it, prepare a new manifest, repeat preflight/soak, and restart the seven-day clock.

Start the official clock at a Berlin midnight. This produces exactly seven complete,
contiguous Berlin-day bundles without treating a partial first or last calendar day as a
full experimental day. Invoke the prepared seven-day launcher during the final ten minutes
before midnight; it waits only for that immediate boundary and fails closed if activation is
outside seconds `00` through `05` or wakes late. Soak and process-drill launches continue to
align to the next safe minute instead.

```bash
scripts/start-paper-week.sh \
  artifacts/manifests/WEEK_MANIFEST.json \
  artifacts/restore-drills/RESTORE/restore-verification.json \
  artifacts/qualification/QUALIFICATION_BUNDLE \
  artifacts/readiness/SOAK_READINESS_BUNDLE
```

After the seventh Berlin day has ended, keep the worker running while generating that
day's bundle, then freeze the final aggregate:

```bash
scripts/daily-paper-ops.sh EXPERIMENT_ID DAY_7_YYYY_MM_DD
uv run maais final-report \
  --experiment EXPERIMENT_ID \
  --start-date DAY_1_YYYY_MM_DD \
  --reports artifacts/reports \
  --output artifacts/final-reports
scripts/stop-paper-week.sh
```

The final command requires exactly one complete, hash-valid bundle for each of seven
contiguous dates. It rejects partial days, candidate identity drift, Berlin-window
mismatches, ledger failures, artifact tampering, and account discontinuities. The stop
command may preserve an additional explicitly marked partial day after the official
period; that partial bundle is not included in the final aggregate.

## What the first week can establish

The week evaluates operational correctness, data quality, decision traceability, simulated execution behavior, cost sensitivity, and whether the system generates enough directional opportunities to study. It cannot establish durable profitability or live execution performance. Warm-up and a small sample must not be interpreted as a strategy result.

The week also does not validate exchange liquidation behavior. Its immutable manifest
uses 1x leverage, a fixed maintenance-margin rate of 0.5% of gross notional, no liquidation
price model, and no claim of exchange liquidation parity. Those assumptions must remain
visible in Mission Control and identical across all daily and final reports. Exchange-tiered
margin and liquidation modeling are a later research gate before any real-capital proposal.

## Exit evidence

Retain the qualification bundle and every hashed check log, manifest, preflight
output, run-state/logs, seven daily report bundles, the
immutable final report bundle, daily backups, incident notes, restart evidence, final
ledger verification, and restored-backup verification. Aggregate only frozen daily
snapshots; do not rewrite them.
