# Railway production-paper preflight

Production means public live market data plus the local paper broker. It never means live
money or authenticated exchange execution.

## Standby deployment

Promote the exact qualified candidate into the isolated production environment. Use one EU
West replica per role, private PostgreSQL/worker/operations/verifier networking, a public
domain only for web, restart `NEVER`, no sleep, zero overlap, and autodeploy disabled after
the frozen deployment. Do not reuse an older `main` build.

Create a fresh run and manifest identity with no prior decisions. Worker and operations
remain standby. Capture provider facts under one operation ID into a canonical snapshot;
evaluation must not call mutable Railway, Sentry, storage, or cost APIs implicitly.

Run and dual-store preflight:

```bash
uv run maais cloud-preflight --candidate-hash CANDIDATE_HASH --run RUN_ID --experiment EXPERIMENT_ID --manifest-hash MANIFEST_HASH --environment production --local-preflight artifacts/cloud/local-preflight.json --snapshot artifacts/cloud/provider-snapshot.json --output artifacts/cloud/preflight
```

The report preserves all 16 local gate names and appends Railway identity, EU single-replica
topology, private services, database role probes, operator auth, redaction, Sentry,
independent monitors, dual-store retention, audit chain, cloud registry,
restart/sleep/autodeploy, and cost headroom gates. Missing, stale, duplicate, unknown,
cross-run, cross-candidate, provider-unavailable, or hash-invalid evidence fails closed.

Inspect the dual-store publication record and every gate. A local JSON file, green health
response, screenshot, CI run, or deployed website is not a passing cloud preflight.

If all gates pass, keep standby and ask exactly: “Authorize activating this exact run for
the uninterrupted 24-hour Railway paper soak?” Deployment approval does not answer that
question.
