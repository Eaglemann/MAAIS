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

## Audited operator transitions

Mission Control remains read-only. After reviewing the immutable evidence, acknowledge the exact incident within the expected experiment:

```bash
uv run maais acknowledge-incident \
  --experiment EXPERIMENT_ID \
  --incident INCIDENT_ID \
  --actor denis
```

Acknowledgement records who saw the incident but deliberately keeps experiment health critical. Resolve only after establishing the cause and outcome from subsequent cycles, logs, ledger verification, and any affected simulated position or order:

```bash
uv run maais resolve-incident \
  --experiment EXPERIMENT_ID \
  --incident INCIDENT_ID \
  --actor denis \
  --resolution "specific evidence-based resolution" \
  --confirm-reviewed
```

Both commands require exact experiment and incident UUIDs and append versioned domain events plus outbox entries. The resolution text is permanent audit evidence: do not use vague text such as `fixed`, and do not resolve a continuing condition. Run `verify-ledger` and `health` again afterward.
