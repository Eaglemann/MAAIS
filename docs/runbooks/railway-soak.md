# Railway 24-hour paper soak

This run starts only after passing production preflight and explicit activation approval.
No code, variable, deployment, scale, schema, cluster, threshold, monitor, or service
configuration may change during the clock.

Record the activation time in UTC and Europe/Berlin plus candidate, run, experiment,
manifest, schema, cluster, deployment, replica, region, and one boot ID per required role.
Persist minute internal health and independent uptime samples. Logs and Sentry supplement
PostgreSQL; the event ledger and immutable artifacts remain authoritative.

Observe decisions, rejections, proposals, orders, fills, counterfactuals, horizon progress,
rationale/lineage completeness, cursors, queue capacity, audit chain, incidents, artifact
replication, and resource/cost headroom. The first 60 prior bars per symbol may be warm-up
quarantine or neutral decisions. Zero fills alone is not a failure and never authorizes a
threshold change.

Any boot, redeploy, replacement, restart, recovery, scale, configuration, schema, cluster,
candidate, or manifest change permanently interrupts the candidate. Preserve evidence and
do not recover inside the same timed run.

After Berlin midnight, require one reconciled daily report and post-close logical backup in
both stores. At or after 24 hours, the read-only verifier freezes observations and runs:

```bash
uv run maais cloud-soak-verdict --candidate-hash CANDIDATE_HASH --run RUN_ID --experiment EXPERIMENT_ID --manifest-hash MANIFEST_HASH --environment production --local-soak artifacts/cloud/local-soak.json --snapshot artifacts/cloud/soak-snapshot.json --output artifacts/cloud/readiness
```

The command refuses early execution and cannot start, stop, restart, or recover a service.
It publishes the immutable verdict and leaves the run unchanged. Stop at the boundary; a
passing verdict is not authorization for the seven-day paper test.
