# Railway qualification

This workflow proves one exact Git commit before any official timed paper run. It is
paper-only. Provider actions are qualification mutations, not authorization to start a
24-hour soak or the seven-day test.

## Frozen candidate

1. Confirm the feature branch, pushed `HEAD`, and both exact-SHA GitHub CI runs match.
2. Build the dashboard and candidate descriptor from the clean commit:

   ```bash
   npm --prefix dashboard ci
   npm --prefix dashboard run build
   uv run maais candidate-descriptor --repository . --dashboard-dir dashboard/dist --git-sha GIT_SHA --source-clean true --output artifacts/candidates/candidate.json
   ```

3. Use one GitHub-connected image for every role. Set `/railway/web.toml`,
   `/railway/worker.toml`, `/railway/operations.toml`, `/railway/migrator.toml`, or
   `/railway/verifier.toml` as the service's absolute config path. Region is EU West,
   replicas are one, overlap is zero, sleep is off, and restart policy is `NEVER`.
4. Configure variable names exactly as specified in
   [railway-variables.md](railway-variables.md). The operator enters sealed secrets in
   provider consoles; never put values in commands, logs, artifacts, screenshots, or chat.

## Authorized qualification boundary

Before every provider mutation, present the environment, service, deployment, intended
change, and expected evidence. Continue only after explicit operator approval. Capture
only returned non-secret identities and hashes.

In order:

1. Provision isolated qualification PostgreSQL and artifact targets.
2. Run `uv run maais cloud-bootstrap-roles --expected-revision 0022` using the
   one-time admin connection. The command creates principals first, migrates under
   `maais_migrator`, then finalizes runtime grants before removing bootstrap secrets.
3. Run `uv run maais cloud-migrate --expected-revision 0022` as `maais_migrator`.
4. Deploy web, worker, operations, and verifier from the same commit in standby. Only web
   receives a public domain.
5. Verify runtime candidate, schema, cluster, role, deployment, replica, region, auth,
   CSRF, telemetry redaction, Sentry delivery, storage version/retention, and audit chain.
6. Publish and read back disposable evidence from the Railway replica and canonical WORM
   store.
7. Create a disposable standby run and separately approve each controlled replacement or
   outage. The evaluator never performs provider actions itself.

Freeze the observations and publish the result:

```bash
uv run maais cloud-process-drill-verdict --candidate-hash CANDIDATE_HASH --run RUN_ID --experiment EXPERIMENT_ID --manifest-hash MANIFEST_HASH --environment qualification --snapshot artifacts/cloud/drills-snapshot.json --output artifacts/cloud/process-drills
```

The required drills are Mission Control replacement, strict worker lease takeover,
operations daily-close replacement, database interruption, both artifact-target failures,
Sentry outage fallback, and restore into a fresh database. Any overlap, duplicate trade
state, missing alert/incident, non-idempotent retry, or reconciliation failure fails.

Stop the disposable qualification run after evidence is published. Preserve the restored
database and evidence until separately authorized cleanup.
