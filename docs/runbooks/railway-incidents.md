# Railway incidents and logs

Structured JSON logs carry event, severity, timestamp, environment, role, release,
deployment/replica/boot, pseudonymous run/experiment references, outcome, and bounded reason
codes. Redaction removes credentials, cookies, authorization headers, DSNs, account data,
and raw provider errors before logs or Sentry. Sentry default PII and session replay remain
disabled.

Use Railway logs for process chronology, Sentry for grouped exceptions and Cron delivery,
Mission Control for the query/read model, PostgreSQL for authoritative incident/audit/run
state, and immutable bundles for reports and restore evidence. No one surface substitutes
for the others.

For each incident:

1. Record its stable ID, first/last occurrence, severity, role, release, deployment, boot,
   reason code, run impact, and evidence hashes.
2. Compare internal minute health with independent uptime and Sentry delivery. A missing
   sample is a failure, not implied health.
3. Check decision/rationale coverage, duplicates, ledger, cursors, queue, daily close, and
   both artifact targets.
4. During a timed run, preserve evidence without recovery or threshold changes.
5. Acknowledge or resolve only after operator review using the audited Mission Control
   action. Never hide an open or operator-review incident to pass readiness.

The sole operator receives uptime, Sentry error, and Sentry Cron notifications. Test events
must contain no sensitive data and may run only outside a timed candidate:

```bash
uv run maais sentry-test-event
```

If Sentry is unavailable, JSON logs, PostgreSQL incidents, independent uptime, and artifact
publication evidence remain available. The outage itself is captured and fails the relevant
qualification or readiness gate.
