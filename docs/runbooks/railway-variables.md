# Railway variable contract

This is the sealed-variable contract for the MAAIS qualification and production-paper
environments. It defines names and authority boundaries only. It is not authorization to
create services, enter variables, deploy a candidate, or start a timed run.

Railway makes service variables available to both the image build and the running
container. Keep each variable service-scoped unless this runbook explicitly says that it
is shared. Review staged changes before applying them; never copy provider output into
source files, shell history, logs, evidence, or chat. See Railway's official
[variable documentation](https://docs.railway.com/variables) and
[system-variable reference](https://docs.railway.com/variables/reference).

Classifications used below:

- **Public metadata** may appear in configuration evidence, but it must still be frozen
  for an official candidate.
- **Sealed secret** is entered or referenced only in the provider UI and must never be
  read back by an agent or written to an artifact.
- **Provider-injected** is supplied by Railway and must not be copied or overridden.

Each service must select its custom config path in Railway Settings:
`/railway/web.toml`, `/railway/worker.toml`, `/railway/operations.toml`,
`/railway/migrator.toml`, or `/railway/verifier.toml`. Config-as-code values override
dashboard values for a deployment, as described by Railway's
[config-as-code reference](https://docs.railway.com/config-as-code/reference).

## Shared candidate and runtime metadata

Set the following on all five MAAIS services. Use one value per environment so every
role builds the same candidate assets and descriptor.

| Variable | Classification | Required value or source |
| --- | --- | --- |
| `RUN_MODE` | Public metadata | Exactly `paper_live` |
| `ENVIRONMENT` | Public metadata | Exactly `qualification` or `production` |
| `MAAIS_DEPLOYMENT_TARGET` | Public metadata | Exactly `railway` |
| `MAAIS_SERVICE_ROLE` | Public metadata | One of `web`, `worker`, `operations`, `migrator`, or `verifier`, matching the service |
| `MAAIS_EXPECTED_RAILWAY_REGION` | Public metadata | Exactly `europe-west4-drams3a` |
| `MAAIS_EXPECTED_SCHEMA_REVISION` | Public metadata | Exactly `0022` for this migration head |
| `MAAIS_DATABASE_ROLE_NAME` | Public metadata | `maais_web`, `maais_worker`, `maais_ops`, `maais_migrator`, or `maais_verifier`, matching the service |
| `MAAIS_CANDIDATE_DESCRIPTOR_PATH` | Public metadata | Exactly `/app/candidate.json` |
| `MAAIS_LOG_FORMAT` | Public metadata | Exactly `json` |
| `MAAIS_SOURCE_CLEAN` | Public build assertion | Exactly `true`; never set it for a non-Git or dirty source context |
| `VITE_SENTRY_ENVIRONMENT` | Public build metadata | Same value as `ENVIRONMENT` |

Railway must inject these values into all five GitHub-connected deployments. Do not
create user variables with these names:

| Variable | Classification | Use |
| --- | --- | --- |
| `RAILWAY_PROJECT_ID` | Provider-injected | Runtime identity |
| `RAILWAY_ENVIRONMENT_ID` | Provider-injected | Runtime identity |
| `RAILWAY_SERVICE_ID` | Provider-injected | Runtime identity |
| `RAILWAY_DEPLOYMENT_ID` | Provider-injected | Runtime identity |
| `RAILWAY_SNAPSHOT_ID` | Provider-injected when available | Runtime identity |
| `RAILWAY_REPLICA_ID` | Provider-injected | Service-boot identity |
| `RAILWAY_REPLICA_REGION` | Provider-injected | Must equal the frozen expected region |
| `RAILWAY_GIT_COMMIT_SHA` | Provider-injected for a GitHub deployment | Exact 40-character candidate commit and image release |

`RAILWAY_GIT_COMMIT_SHA` is not guaranteed for non-Git deploys, so official candidates
must come from the connected GitHub commit. Do not manually imitate a missing provider
identity. Railway variables are injected into Docker builds only when the Dockerfile
declares the corresponding `ARG`; see the official
[Dockerfile documentation](https://docs.railway.com/builds/dockerfiles).

## Web service

Only the web service may receive these variables:

| Variable | Classification | Requirement |
| --- | --- | --- |
| `DATABASE_URL` | Sealed secret | Private `postgresql+psycopg` URL for `maais_web`; never the PostgreSQL admin URL |
| `MAAIS_AUTH_MODE` | Public metadata | Exactly `operator_session` |
| `MAAIS_OPERATOR_PASSWORD_HASH` | Sealed secret | Policy-compliant Argon2id hash generated interactively |
| `MAAIS_SESSION_PEPPER` | Sealed secret | Independent generated token |
| `MAAIS_CSRF_PEPPER` | Sealed secret | Independent generated token |
| `MAAIS_MONITOR_TOKEN` | Sealed secret | Independent generated token used only by `/monitor/v1/health` |
| `MAAIS_OPERATOR_SECURE_COOKIES` | Public metadata | Exactly `true` |
| `MAAIS_OPERATOR_PUBLIC_ORIGIN` | Public metadata | One canonical Railway HTTPS origin, with no path or trailing slash |
| `PORT` | Provider-injected | Railway's web listener port; do not override |

Run the password hash command personally in a private interactive terminal:

```bash
uv run maais operator-password-hash
```

Enter the passphrase only at the hidden prompts and paste only the resulting hash directly
into the sealed Railway field. The operator password itself is never stored.

Run the token generator three separate times and paste each result directly into exactly
one of the three token fields:

```bash
uv run maais generate-secret-token
```

Never reuse a token. Never paste secret values into chat. Clerk is not part of this
single-operator authentication boundary and no Clerk variables are used.

## Worker service

Only the worker service may receive:

| Variable | Classification | Requirement |
| --- | --- | --- |
| `DATABASE_URL` | Sealed secret | Private `postgresql+psycopg` URL for `maais_worker`; never an admin or operations URL |
| `MAAIS_RUN_ID` | Public immutable identity | Exact non-nil UUID of the activated run |
| `MAAIS_MANIFEST_ARTIFACT_ID` | Public immutable identity | Exact non-nil catalog UUID for the frozen manifest artifact |
| `MAAIS_ARTIFACT_STORE_MODE` | Public metadata | Exactly `canonical_read` |

The worker also receives the canonical-store variables below, but with a distinct
read-only credential that can read exact versions and cannot put, overwrite, delete,
change retention, or administer the bucket. It receives no Railway replica credentials.

## Operations service

Only the operations service may receive:

| Variable | Classification | Requirement |
| --- | --- | --- |
| `DATABASE_URL` | Sealed secret | Private `postgresql+psycopg` URL for `maais_ops`; never an admin or worker URL |
| `MAAIS_RUN_ID` | Public immutable identity | Exact non-nil UUID of the activated run |
| `MAAIS_ARTIFACT_STORE_MODE` | Public metadata | Exactly `dual_s3` |
| `MAAIS_SENTRY_DAILY_CLOSE_MONITOR_SLUG` | Public metadata | Dedicated qualification or production Sentry monitor slug |
| `MAAIS_SENTRY_BACKUP_MONITOR_SLUG` | Public metadata | Distinct dedicated monitor slug |
| `MAAIS_SENTRY_EVIDENCE_MONITOR_SLUG` | Public metadata | Distinct dedicated monitor slug |

Operations is the only continuously running role with Railway replica write authority and
canonical WORM publication authority. The three monitor slugs must be distinct and must
not be configured on any other role.

## Migrator service

Only the migrator service may receive:

| Variable | Classification | Requirement |
| --- | --- | --- |
| `DATABASE_URL` | Sealed secret | Private `postgresql+psycopg` URL for `maais_migrator`; never the provider admin URL |

The migrator receives no run ID, manifest ID, web authentication secret, artifact-store
credential, or Cron monitor. Its command is fixed to revision `0022` and its Railway
restart policy is `NEVER`.

The one-time role bootstrap receives the PostgreSQL administrator URL plus five independent
sealed passwords. Enter them directly in the migrator Variables UI, run bootstrap once,
then remove the administrator URL and all five bootstrap password variables before the
ordinary migrator deployment:

| Variable | Classification | Purpose |
| --- | --- | --- |
| `MAAIS_MIGRATOR_DATABASE_PASSWORD` | Sealed secret | Password for `maais_migrator` |
| `MAAIS_WORKER_DATABASE_PASSWORD` | Sealed secret | Password for `maais_worker` |
| `MAAIS_WEB_DATABASE_PASSWORD` | Sealed secret | Password for `maais_web` |
| `MAAIS_OPERATIONS_DATABASE_PASSWORD` | Sealed secret | Password for `maais_ops` |
| `MAAIS_VERIFIER_DATABASE_PASSWORD` | Sealed secret | Password for `maais_verifier` |

Never share or reuse these values. Runtime services receive only their role-specific
private connection URL, assembled as a Railway variable reference in the provider UI.

## Verifier service

Only the verifier service may receive:

| Variable | Classification | Requirement |
| --- | --- | --- |
| `DATABASE_URL` | Sealed secret | Private `postgresql+psycopg` URL for `maais_verifier` |
| `MAAIS_RUN_ID` | Public immutable identity | Exact non-nil UUID of the run being verified |

The verifier receives no web authentication secret or artifact-store credential. Its
database role is read-only and its one-shot start command reads `MAAIS_RUN_ID` directly
from validated settings without shell expansion.

## Railway replica store

Configure these variables on operations only. Use Railway variable references to the
qualification or production bucket instead of copying credential values. Railway's
current bucket variables and S3 compatibility are documented in the official
[storage bucket reference](https://docs.railway.com/storage-buckets).

| MAAIS variable | Classification | Railway bucket reference |
| --- | --- | --- |
| `MAAIS_ARTIFACT_REPLICA_ENDPOINT_URL` | Public metadata | Bucket `ENDPOINT` |
| `MAAIS_ARTIFACT_REPLICA_REGION` | Public metadata | Bucket `REGION` |
| `MAAIS_ARTIFACT_REPLICA_BUCKET` | Public metadata | Bucket `BUCKET` (not `RAILWAY_BUCKET_NAME`) |
| `MAAIS_ARTIFACT_REPLICA_ACCESS_KEY` | Sealed secret | Bucket `ACCESS_KEY_ID` |
| `MAAIS_ARTIFACT_REPLICA_SECRET_KEY` | Sealed secret | Bucket `SECRET_ACCESS_KEY` |
| `MAAIS_ARTIFACT_REPLICA_SESSION_TOKEN` | Sealed secret, optional | Leave absent unless the provider issues a temporary session token |

Railway buckets currently do not provide object versioning or Object Lock. They are an
operational replica only and can never replace the canonical archive.

## Canonical WORM store

Configure these variables on worker and operations. Use different provider credentials
for the two roles: read-only exact-version access for worker, and narrowly scoped
put/read/retention access for operations.

| Variable | Classification | Requirement |
| --- | --- | --- |
| `MAAIS_ARTIFACT_CANONICAL_ENDPOINT_URL` | Public metadata | Canonical HTTPS S3 endpoint without credentials or a path |
| `MAAIS_ARTIFACT_CANONICAL_REGION` | Public metadata | Canonical bucket region |
| `MAAIS_ARTIFACT_CANONICAL_BUCKET` | Public metadata | Dedicated environment-specific bucket name |
| `MAAIS_ARTIFACT_CANONICAL_ACCESS_KEY` | Sealed secret | Role-specific access key |
| `MAAIS_ARTIFACT_CANONICAL_SECRET_KEY` | Sealed secret | Role-specific secret key |
| `MAAIS_ARTIFACT_CANONICAL_SESSION_TOKEN` | Sealed secret, optional | Set only when temporary credentials require it |
| `MAAIS_ARTIFACT_CANONICAL_OBJECT_LOCK_REQUIRED` | Public metadata | Exactly `true` |

Qualification and production must use separate buckets and credentials. Versioning,
Object Lock, retention mode, write-once behavior, exact-version reads, and read-back hash
verification must pass before qualification evidence is accepted.

## Backend Sentry

Configure the following on all five roles:

| Variable | Classification | Requirement |
| --- | --- | --- |
| `SENTRY_DSN` | Sealed provider field | Backend project DSN entered directly in Railway |
| `SENTRY_TRACES_SAMPLE_RATE` | Public metadata | Start at `0.0`; change only through a new qualified candidate |
| `SENTRY_PROFILES_SAMPLE_RATE` | Public metadata | Start at `0.0` |
| `SENTRY_SEND_DEFAULT_PII` | Public safety control | Exactly `false` |
| `MAAIS_SENTRY_SESSION_REPLAY_ENABLED` | Public safety control | Exactly `false` |

`SENTRY_AUTH_TOKEN` is a source-map upload credential and belongs in GitHub Actions only.
It must never exist in Railway, an image layer, a runtime service, chat, or an evidence
artifact. The associated Sentry organization and upload project metadata also stay in the
guarded GitHub release job.

## Public browser Sentry

`VITE_SENTRY_DSN` is public metadata because it is compiled into browser JavaScript. Set
the same browser DSN on all five services so independently built role images produce the
same dashboard asset hash and candidate descriptor; only the web role serves those
assets. Set `VITE_SENTRY_ENVIRONMENT` on all roles as described in the shared section.
`VITE_SENTRY_RELEASE` is derived inside the Dockerfile from provider-injected
`RAILWAY_GIT_COMMIT_SHA`; do not set it manually.

The browser DSN is not the upload token. Session replay and default PII remain disabled,
and production source maps are uploaded only by the guarded GitHub Actions release job.

## Explicitly absent variables

The official paper workflow must not define `BINANCE_DEMO_API_KEY` or
`BINANCE_DEMO_API_SECRET` on any service. It also requires no live-money, production
exchange, Docker registry, macOS Keychain, Clerk, Railway API-token, or Railway CLI-token
variable. Demo/testnet credentials, if separately approved later, belong to an isolated
non-official protocol smoke environment and never contribute to paper P&L.

Before any provider change, compare the staged variable-name inventory with this runbook.
Evidence may record names, classifications, configured/not-configured booleans, and hashes
of canonical redacted inventories. It must never record secret values or provider-returned
credential material.
