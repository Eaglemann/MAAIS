# Railway recovery

Recovery is for an already failed or non-timed standby/qualification service. Any recovery
during an official soak or seven-day test invalidates that candidate even if health later
turns green.

## First response

1. Do not restart immediately. Record environment, service, deployment, replica, boot,
   candidate, run, schema, and cluster identities plus the last healthy sequence.
2. Preserve structured logs, Sentry event references, health/audit rows, incident IDs,
   queue/cursor state, artifact publication attempts, and current ledger verification.
3. Confirm no live-money mode or exchange credential is present.
4. Classify provider outage, application failure, database interruption, artifact target,
   auth, Sentry, resource/cost, or operator action.

For an official timed run, mark interruption and stop. Do not acknowledge/resolve incidents,
change configuration, redeploy, scale, or recover as part of verdict collection.

## Authorized standby or qualification recovery

Present the exact mutation and receive explicit operator approval. Require the previous
instance to be stopped, then capture the replacement deployment/replica/boot identity.
Worker takeover must acquire a strictly higher lease epoch. Reconcile decisions, orders,
fills, counterfactuals, checkpoints, projections, audit chain, incidents, reports, and
artifacts before declaring recovery.

Restore only an exact cataloged canonical backup version into a fresh secret target URL:

```bash
uv run maais cloud-restore-verify --artifact-record ARTIFACT_RECORD_ID --output artifacts/cloud/restores
```

Never overwrite production PostgreSQL, reset its volume, accept an unversioned object, or
delete the restored database automatically. A failed attempt remains evidence; use a new
fresh target after review.
