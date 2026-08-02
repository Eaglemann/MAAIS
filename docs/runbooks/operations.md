# Paper-week operations

## Start

Use the exact candidate manifest and a passing restore-verification artifact:

```bash
scripts/start-paper-week.sh \
  artifacts/manifests/week-candidate-YYYY-MM-DD.json \
  artifacts/restore-drills/<drill>/restore-verification.json
```

The script starts PostgreSQL, applies migrations, runs fail-closed preflight, starts Mission Control on localhost, starts the paper worker, and waits for a running checkpoint plus active lease. Process IDs, immutable inputs, preflight output, and logs are recorded under `artifacts/run-state/`.

Mission Control is available at [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Status

```bash
scripts/status-paper-week.sh
uv run maais verify-ledger
```

Review Mission Control at least twice daily. Confirm:

- worker checkpoint is `running` and lease is `active`;
- expected cursor count equals configured symbols and no cursor is halted;
- no unresolved operator-review incident exists;
- kill switch is inactive;
- decision counts, proposal/order/fill counts, account values, and source timestamps are plausible;
- warm-up `insufficient_history` is distinguished from real quality failures.

## Daily close

After a Berlin calendar day has ended, run:

```bash
scripts/daily-paper-ops.sh EXPERIMENT_ID YYYY-MM-DD
```

This verifies the ledger, writes immutable Markdown/JSON/CSV/Parquet report artifacts with hashes, creates a validated PostgreSQL backup, and checks the running processes/API. Never overwrite or delete a daily bundle during the experiment.

## Stop

```bash
scripts/stop-paper-week.sh
```

The stop script sends `SIGINT` to allow checkpoint and lease release, refuses to force-kill a stuck worker, verifies the ledger, writes a final partial-day report, creates a backup, and preserves stopped-run state.
