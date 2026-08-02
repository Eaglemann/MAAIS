# Recovery and restore

## Worker or machine restart

1. Confirm the prior worker process is no longer alive.
2. Run `uv run maais verify-ledger`.
3. Inspect the latest checkpoint, lease, cursors, recovery runs, incidents, open positions, pending orders, and kill switch in Mission Control.
4. Start with the same manifest and restore-verification artifact. The worker must restore persisted history/cursors and enforce exactly-once decision keys.
5. Compare counts and event positions before and after restart. Any duplicate decision, order, fill, or report fails the candidate.

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
