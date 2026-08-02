# Seven-day paper experiment protocol

## Entry gates

The seven-day clock starts only after all of the following have fresh evidence:

- clean committed candidate and immutable manifest;
- full unit, PostgreSQL integration, static, secret, dependency, frontend, and browser checks;
- deterministic replay and fault-injection suite;
- validated database backup and passing restore drill;
- 24-hour live-data soak with no unexplained state, duplicates, unhandled exceptions, or unrecovered gaps;
- Mission Control and daily report reconciliation;
- no live-money path or exchange credentials.

The required fault matrix and process-drill boundaries are defined in
`docs/testing/fault-injection.md`. Process-kill drills use a disposable candidate and are
completed before the clean 24-hour soak begins.

## During the run

- Do not change code, dependencies, manifest, thresholds, symbols, agent weights, fees, latency, or risk settings.
- Generate one immutable report and backup for each completed Berlin day.
- Review incidents and decision explanations without editing historical records.
- Machine sleep, network outage, worker restart, and API interruption must be logged. Recovery must preserve exactly-once identities.
- A material defect, ledger mismatch, duplicate, missing protection, or configuration/code change ends the candidate. Fix it, commit it, prepare a new manifest, repeat preflight/soak, and restart the seven-day clock.

Start the official clock at a Berlin midnight. This produces exactly seven complete,
contiguous Berlin-day bundles without treating a partial first or last calendar day as a
full experimental day.

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

## Exit evidence

Retain the manifest, preflight output, run-state/logs, seven daily report bundles, the
immutable final report bundle, daily backups, incident notes, restart evidence, final
ledger verification, and restored-backup verification. Aggregate only frozen daily
snapshots; do not rewrite them.
