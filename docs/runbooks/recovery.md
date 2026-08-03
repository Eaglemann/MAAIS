# Recovery and restore

## Worker or machine restart

For the intentional pre-soak worker and API kill tests, use
`scripts/run-process-drills.sh` rather than manually assembling evidence. The
steps below are the supported incident recovery path for an unplanned failure.

1. Confirm the prior worker process is no longer alive.
2. Run `uv run maais verify-ledger`.
3. Inspect the latest checkpoint, lease, cursors, recovery runs, incidents, open positions, pending orders, and kill switch in Mission Control.
4. Run `scripts/recover-paper-week.sh worker "REASON"`. It refuses a live recorded PID, waits for lease expiry, reuses the frozen manifest, requires a higher lease epoch, restores the sleep inhibitor, verifies the ledger before and after, and writes immutable evidence under `artifacts/run-state/recovery-evidence/`.
5. Compare counts and event positions before and after restart. Any duplicate decision, order, fill, or report fails the candidate.

Recovery reuses the Docker context and PostgreSQL system identifier recorded at
candidate start. Do not switch container engines or remap the database port; a
different cluster is rejected before either service is restarted.

## Mission Control restart

If Mission Control stops but the worker remains alive, run:

```bash
scripts/recover-paper-week.sh dashboard "REASON"
```

The recovery refuses to replace a live recorded dashboard PID, restarts only the read-only
API/UI process with structured logging, verifies health and the ledger, and records the old
and new process identities plus before/after authoritative state. The worker must continue
independently throughout the interruption.

## Daily-close supervisor restart

If the automatic daily-close supervisor exits while the worker and Mission Control remain
healthy, run:

```bash
scripts/recover-paper-week.sh scheduler "REASON"
```

The replacement re-reads the authoritative run state and catches up only completed,
contiguous Berlin days. A gap, duplicate report date, changed report identity, or failed
report/backup/status command makes it exit again instead of skipping evidence. Any such
recovery invalidates an official soak or seven-day candidate; retain the recovery artifact,
fix the cause, and prepare a fresh candidate.

## Database restore drill

Back up:

```bash
uv run maais backup --output backups
```

Restore only into a new database whose name ends in `_restore` or `_test`; the confirmation must match exactly:

```bash
uv run maais restore \
  --backup backups/<bundle> \
  --target-database maais_YYYYMMDD_restore \
  --confirm-target maais_YYYYMMDD_restore \
  --output artifacts/restore-drills
```

The workflow validates the archive hash, creates a new database, restores with ownership/privilege portability, then requires exact schema revision, every table count, and full ledger consistency. It never overwrites the main database and does not delete the restored evidence database automatically.

## Failed restore

Do not reuse a partially restored target. Preserve its name and error output, choose a new suffix-constrained target, and investigate archive/catalog, PostgreSQL version, disk, and permissions. The candidate cannot start without a passing restore artifact.
