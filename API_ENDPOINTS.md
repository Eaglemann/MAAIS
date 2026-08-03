# Mission Control API

## Scope and binding

Mission Control is a local FastAPI service bound to `127.0.0.1:8000` by default.
All API responses disable browser caching. The built dashboard is mounted at `/`;
FastAPI exposes its generated OpenAPI document at `/openapi.json` and interactive
documentation at `/docs` while the service is running.

Read endpoints execute in PostgreSQL read-only transactions. The only write API
enqueues an immutable operator command; the paper worker, not the API, validates
and applies official state changes.

## Authentication

Read endpoints are unauthenticated because the service is loopback-only. Command
creation requires:

```http
Authorization: Bearer <local Mission Control token>
Content-Type: application/json
```

The token is generated locally, stored in an ignored mode-`0600` file, and never
belongs in source control, logs, screenshots, or documentation. Invalid or absent
tokens return `401` with `WWW-Authenticate: Bearer`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Read-only database transaction check, service status, schema revision, and timestamp. |
| `GET` | `/api/v1/experiments` | List recent experiments; `limit` is 1–200, default 50. |
| `GET` | `/api/v1/experiments/{experiment_id}/overview` | Experiment identity, frozen model assumptions, account, runtime lease/checkpoint, decision totals, operations, freshness, positions, orders, and incidents. |
| `GET` | `/api/v1/experiments/{experiment_id}/decisions` | Cursor-paginated audit ledger with symbol/status/disposition/reason filters. |
| `GET` | `/api/v1/decisions/{decision_id}` | Complete causal decision: frame, quality, eight agents, summary, gates, proposal, execution, counterfactual, incident, hashes, and timeline. |
| `GET` | `/api/v1/experiments/{experiment_id}/trades` | Cursor-paginated proposals with linked decision and execution state. |
| `GET` | `/api/v1/experiments/{experiment_id}/research` | Equity/drawdown, costs, distributions, calibration, attribution, benchmarks, gate value, counterfactuals, and execution sensitivities. |
| `POST` | `/api/v1/experiments/{experiment_id}/commands` | Authenticated, idempotent append to the operator command inbox; returns `202`. |
| `GET` | `/api/v1/experiments/{experiment_id}/commands` | List command lifecycle records, optionally filtered by status. |
| `GET` | `/api/v1/commands/{command_id}` | Retrieve one command and its requested/accepted/completed/rejected evidence. |
| `GET` | `/api/v1/events` | Poll the ordered outbox after a numeric cursor. |
| `WS` | `/api/v1/events/stream` | Outbox catch-up plus live polling and ten-second heartbeat. |

## Pagination and filters

Decision and trade pages use stable keyset pagination. Supply both values returned
by the previous page when continuing:

- `before_at`: UTC timestamp;
- `before_id`: UUID.

Decision filters are `symbol`, `status`, `disposition`, and `reason_code`.
Trade filters are `symbol`, `proposal_status`, and `decision_disposition`.
Limits are 1–500, default 100.

The outbox endpoint accepts `after_cursor` (nonnegative, default 0) and `limit`
(1–1000, default 500). It returns `items`, `next_cursor`, and `has_more`. The
WebSocket sends either `{"type":"events", ...}` pages or heartbeat objects.

## Operator commands

Request body:

```json
{
  "command_type": "pause",
  "idempotency_key": "operator-pause-unique-key",
  "reason": "Evidence-backed operator reason",
  "payload": {},
  "confirmation": "CONFIRM PAUSE"
}
```

Command types:

- `start`, `pause`, `resume`, `stop`;
- `emergency_halt`, `flatten`, `reset_kill_switch`;
- `acknowledge_incident`, `resolve_incident`.

Every safety-critical type requires `CONFIRM <COMMAND_TYPE_IN_UPPERCASE>` exactly;
for example, `CONFIRM EMERGENCY_HALT`. Incident acknowledgement is the only type
that is not safety-critical. The caller supplies an idempotency key of 8–128
characters. Reusing it with changed content returns `409`.

The returned command record includes its immutable request hash, operator
confirmation, version, timestamps, accepting worker identity, result, and
rejection information. Enqueueing does not mean execution succeeded; observe the
record until it reaches `completed` or `rejected`.

## Errors

| Status | Meaning |
|---|---|
| `401` | Local bearer token absent or invalid. |
| `404` | Experiment, decision, or command does not exist. |
| `409` | Idempotency/identity conflict. |
| `422` | Invalid UUID/query/body, impossible filter combination, or missing exact confirmation. |
| `503` | Dashboard bundle is absent at `/`; API routes can still expose their own status. |

API error bodies use `{"detail":"..."}`. A successful `/api/v1/health` response
must report `database_transaction` as `read only`; a `200` response alone is not
the full health/readiness verdict.
