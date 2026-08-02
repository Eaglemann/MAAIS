# Incident response

## Severity guide

- Warning: isolated recovered gap, non-required reference issue, or dashboard interruption while the worker remains healthy.
- Error: required source failure, repeated gap, stale/crossed book, halted symbol, database interruption, or operator-review incident.
- Critical: ledger mismatch, account reconciliation failure, protection failure, active kill switch, duplicate order/fill, or inability to persist state.

## Immediate response

1. Do not edit the manifest, database, decision rows, report, or backup.
2. Capture `scripts/status-paper-week.sh`, the relevant Mission Control decision bundle, and the latest logs under `artifacts/run-state/logs/`.
3. If state integrity, persistence, or position protection is uncertain, stop gracefully with `scripts/stop-paper-week.sh`.
4. Run `uv run maais verify-ledger`. Any nonzero result blocks restart.
5. Create a backup before diagnosis that could alter local state.
6. Record the incident time in UTC and Berlin time, affected symbols, reason codes, first/last event IDs, and whether any simulated position or pending order was involved.

Never clear a kill switch or mark an incident resolved merely to resume the timed run. A material fix or configuration change ends the candidate; prepare a new committed candidate and restart the clock.
