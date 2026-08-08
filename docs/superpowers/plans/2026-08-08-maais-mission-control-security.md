# MAAIS Mission Control Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Mission Control safely to the sole operator while protecting every query, export, command, and WebSocket and preserving the current local token-file workflow.

**Architecture:** Local mode continues to use the private bearer token file. Railway mode uses one Argon2id password hash, an opaque high-entropy session token stored only as a secure cookie, a separately rotated CSRF token, PostgreSQL-backed sessions in `maais_auth`, and a global single-operator login throttle. Public endpoints are limited to liveness, readiness, login, and a secret-header monitoring summary; every other API, export, and WebSocket requires a valid session.

**Tech Stack:** FastAPI, Starlette middleware, Argon2id via argon2-cffi, secrets, HMAC/SHA-256 token hashes, SQLAlchemy 2, Alembic, PostgreSQL 16, React 19, TypeScript, Vitest, Testing Library, Playwright, pytest.

## Global Constraints

- The application remains single-operator. Do not add public signup, OAuth, password reset email, organizations, roles, API keys, or multi-user administration.
- Store only keyed hashes of session and CSRF tokens. Never persist or log raw tokens or the operator password.
- The operator password hash is entered directly into Railway as a secret; no plaintext password or hash is committed.
- Railway cookies use `__Host-maais_session`, `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`, and no `Domain`.
- State-changing requests require the session cookie and matching `X-CSRF-Token`; bearer auth is rejected in Railway mode.
- Login responses, auth errors, APIs, and exports use `Cache-Control: no-store`; static hashed assets may use immutable caching.
- WebSocket authentication happens before `accept()`. An unauthenticated socket receives policy close code `1008` without streaming events.
- Do not include IP address, user agent, password length, submitted username, raw path query, session ID, or CSRF token in off-platform telemetry.
- Read authentication never grants trading mutation. Operator actions remain queued audited commands consumed by the worker.
- Public monitoring secret is independent from operator auth and can authorize only `/monitor/v1/health`.

---

## Interfaces Produced

`maais/security/sessions.py` must expose:

```python
class AuthMode(StrEnum):
    LOCAL_TOKEN = "local_token"
    OPERATOR_SESSION = "operator_session"


@dataclass(frozen=True, slots=True)
class OperatorSession:
    id: UUID
    token_hash: str
    csrf_hash: str
    actor: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    version: int

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: OperatorSession
    token: str
    csrf_token: str


class SessionRepository(Protocol):
    async def issue(self, request: NewSessionRequest) -> OperatorSession:
        raise NotImplementedError

    async def authenticate(self, token_hash: str, *, observed_at: datetime) -> OperatorSession:
        raise NotImplementedError

    async def revoke(self, session_id: UUID, *, revoked_at: datetime) -> OperatorSession:
        raise NotImplementedError
```

Session token hashing must use an application pepper stored as `SecretStr`:

```python
def opaque_token_hash(token: str, pepper: SecretStr) -> str:
    return hmac.new(
        pepper.get_secret_value().encode("utf-8"),
        token.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
```

## Task 1: Add Argon2 and Security Configuration

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `maais/config/security.py`
- Modify: `maais/config/settings.py`
- Create: `maais/security/__init__.py`
- Create: `maais/security/passwords.py`
- Modify: `maais/cli.py`
- Test: `tests/unit/security/test_passwords.py`
- Test: `tests/unit/config/test_security_settings.py`

**Consumes:** Operator-created Argon2id hash and independent session/monitor secrets.

**Produces:** Validated auth mode, password verifier, cookie/session policy, and secret-safe configuration.

- [x] Write failing tests proving Railway requires session auth, three independent high-entropy secrets, an Argon2id hash, production secure cookies, bounded TTL, and no secrets in representations.

  ```python
  def test_railway_rejects_local_token_auth() -> None:
      with pytest.raises(ValidationError, match="operator_session"):
          SecuritySettings(
              deployment_target="railway",
              auth_mode="local_token",
          )
  ```

- [x] Write password tests for valid, invalid, malformed, and rehash-needed Argon2id values; all invalid credentials return the same public error code.

- [x] Run focused tests and confirm missing dependency/module failures.

  ```bash
  uv run pytest -q tests/unit/security/test_passwords.py tests/unit/config/test_security_settings.py
  ```

- [x] Add Argon2 from the lockfile-resolved package index.

  ```bash
  uv add argon2-cffi
  ```

- [x] Implement a fixed password policy: Argon2id, minimum memory `65536` KiB, time cost `3`, parallelism `4`, hash length `32`, salt length `16`; accept a stronger existing hash and report `needs_rehash` separately.

  ```python
  PASSWORD_HASHER = PasswordHasher(
      time_cost=3,
      memory_cost=65_536,
      parallelism=4,
      hash_len=32,
      salt_len=16,
      type=Type.ID,
  )
  ```

- [x] Implement `SecuritySettings` with a 12-hour absolute session TTL, 30-minute idle TTL, 15-minute login window, 5 failed attempts, and 30-minute lockout; freeze these for the initial candidate.

- [x] Add `maais operator-password-hash` using `getpass` twice on a real TTY and `maais generate-secret-token` using `secrets.token_urlsafe(32)`. Reject password/secret command-line arguments and redirected password input so values never enter shell history or agent tool output. The runbook instructs the operator to run these commands personally and paste outputs directly into provider secret fields.

- [x] Add tests proving the helper never echoes the passphrase, requires confirmation, enforces the documented minimum passphrase policy, emits a valid Argon2id hash/high-entropy token, and produces no structured log or Sentry event.

- [x] Run tests, audit, Ruff, and Pyright.

  ```bash
  uv run pytest -q tests/unit/security/test_passwords.py tests/unit/config/test_security_settings.py tests/test_settings.py
  uv run pip-audit
  uv run ruff check maais/config maais/security maais/cli.py tests/unit/security tests/unit/config
  uv run pyright maais/config maais/security maais/cli.py
  ```

  Expected: all commands exit `0`.

- [x] Commit.

  ```bash
  git add pyproject.toml uv.lock maais/config/security.py maais/config/settings.py maais/security maais/cli.py tests/unit/security/test_passwords.py tests/unit/config/test_security_settings.py
  git commit -m "build: add operator session security"
  git push origin feat/paper-platform-baseline
  ```

## Task 2: Add Migration 0021 and Session Repository

**Files:**

- Create: `alembic/versions/0021_operator_sessions.py`
- Create: `maais/db/models/auth.py`
- Create: `maais/security/sessions.py`
- Create: `maais/db/repositories/sessions.py`
- Modify: `maais/db/models/__init__.py`
- Modify: `maais/db/unit_of_work.py`
- Modify: `tests/integration/conftest.py`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/unit/security/test_sessions.py`
- Test: `tests/integration/test_operator_sessions.py`

**Consumes:** Security settings and opaque token hashes.

**Produces:** Expiring/revocable operator sessions and a durable global login throttle with no PII in the isolated `maais_auth` schema.

- [ ] Write failing domain tests for token entropy, distinct session/CSRF tokens, UTC timestamps, idle/absolute expiry, rotation, revocation idempotency, and constant public error behavior.

- [ ] Write failing PostgreSQL tests for schema parity, unique hashes, one-row `operator_auth_state`, row-locked failure counting, lockout, successful reset, expired-session rejection, and concurrent logout/authentication.

  ```python
  def test_issued_tokens_are_independent_and_high_entropy() -> None:
      issued = issue_session_tokens()
      assert issued.token != issued.csrf_token
      assert len(base64.urlsafe_b64decode(issued.token + "==")) >= 32
      assert len(base64.urlsafe_b64decode(issued.csrf_token + "==")) >= 32
  ```

- [ ] Run tests and confirm schema/session behavior is absent.

  ```bash
  uv run pytest -q tests/unit/security/test_sessions.py tests/integration/test_operator_sessions.py
  ```

- [ ] Create schema `maais_auth`, then migration `0021` tables `maais_auth.operator_sessions` and singleton `maais_auth.operator_auth_state`. Enforce 64-character hashes, positive versions/counts, lifecycle time order, and active-session indexes; downgrade drops tables before the schema.

  ```python
  revision: str = "0021"
  down_revision: str | None = "0020"
  ```

- [ ] Implement session issuance with `secrets.token_urlsafe(32)`, HMAC hashes, row locks for authentication/rotation/revocation, and `hmac.compare_digest` for CSRF verification.

- [ ] Add `sessions: OperatorSessionRepository` to `UnitOfWorkContext`, add schema-qualified tables to integration cleanup, and update CI head assertion to `0021`.

- [ ] Run migration cycle and all focused tests.

  ```bash
  uv run alembic upgrade head
  uv run pytest -q tests/unit/security/test_sessions.py tests/integration/test_operator_sessions.py
  uv run alembic downgrade 0020
  uv run alembic upgrade head
  uv run pytest -q tests/integration/test_operator_sessions.py
  ```

  Expected: head is `0021`; all tests pass.

- [ ] Commit.

  ```bash
  git add alembic/versions/0021_operator_sessions.py maais/db/models/auth.py maais/security/sessions.py maais/db/repositories/sessions.py maais/db/models/__init__.py maais/db/unit_of_work.py tests/integration/conftest.py tests/unit/security/test_sessions.py tests/integration/test_operator_sessions.py .github/workflows/ci.yml
  git commit -m "feat: add operator session persistence"
  git push origin feat/paper-platform-baseline
  ```

## Task 3: Add Server-Side Authentication and CSRF Dependencies

**Files:**

- Create: `maais/api/security.py`
- Modify: `maais/api/schemas.py`
- Modify: `maais/api/app.py`
- Test: `tests/unit/api/test_session_auth.py`
- Test: `tests/integration/test_mission_control_auth_api.py`
- Modify: `tests/unit/api/test_control_auth.py`

**Consumes:** Existing local control token path, `SecuritySettings`, session repository, and Mission Control app factory.

**Produces:** Login/session/CSRF-bootstrap/logout endpoints, secure cookie handling, principal/CSRF dependencies, and explicit local/session modes.

- [ ] Extend `create_app` tests first. The app factory must accept injected settings and clock without reading global secrets in tests.

  ```python
  def create_app(
      session_factory: SessionFactory | None = None,
      *,
      dashboard_dir: Path | None = None,
      control_token: str | None = None,
      control_token_file: Path | None = None,
      security_settings: SecuritySettings | None = None,
      clock: Callable[[], datetime] | None = None,
  ) -> FastAPI:
      raise NotImplementedError
  ```

- [ ] Write failing API tests for successful login, uniform invalid-login response, lockout, secure cookie attributes, session view, authenticated CSRF bootstrap/rotation, logout, expired cookie, CSRF missing/mismatch, bearer rejection in cloud mode, and local token compatibility.

  ```python
  def test_cloud_login_sets_host_only_secure_cookie(client: TestClient) -> None:
      response = client.post("/api/v1/auth/login", json={"password": "correct horse battery staple"})
      cookie = response.headers["set-cookie"]
      assert "__Host-maais_session=" in cookie
      assert "HttpOnly" in cookie
      assert "Secure" in cookie
      assert "SameSite=strict" in cookie
      assert "Path=/" in cookie
      assert "Domain=" not in cookie
  ```

- [ ] Run API tests and confirm the endpoints/dependencies are absent.

  ```bash
  uv run pytest -q tests/unit/api/test_session_auth.py tests/integration/test_mission_control_auth_api.py tests/unit/api/test_control_auth.py
  ```

- [ ] Implement `POST /api/v1/auth/login`, `GET /api/v1/auth/session`, `POST /api/v1/auth/csrf`, and `POST /api/v1/auth/logout`. Login accepts only `{password: str}` with a bounded maximum length and never echoes it. The CSRF bootstrap requires a valid session plus frozen Origin/Host and rotates only the CSRF hash; it grants no domain action.

- [ ] Implement `OperatorPrincipal`, `require_operator`, and `require_csrf`; session lookup reads the cookie and CSRF reads only `X-CSRF-Token`.

  ```python
  @dataclass(frozen=True, slots=True)
  class OperatorPrincipal:
      actor: str
      session_id: UUID | None
      auth_mode: AuthMode
  ```

- [ ] Issue a fresh session and CSRF pair at every successful login and revoke any prior active operator sessions in the same transaction. On browser reload, rotate only CSRF through `/api/v1/auth/csrf`; never attempt to recover a raw CSRF token from its stored hash.

- [ ] Re-run API tests plus existing Mission Control tests.

  ```bash
  uv run pytest -q tests/unit/api tests/integration/test_mission_control_auth_api.py tests/integration/test_mission_control_api.py tests/integration/test_mission_control_commands_api.py
  uv run ruff check maais/api maais/security tests/unit/api tests/integration/test_mission_control_auth_api.py
  uv run pyright maais/api maais/security
  ```

  Expected: all commands exit `0`; local tests continue using the token file without Railway secrets.

- [ ] Commit.

  ```bash
  git add maais/api/security.py maais/api/schemas.py maais/api/app.py tests/unit/api/test_session_auth.py tests/integration/test_mission_control_auth_api.py tests/unit/api/test_control_auth.py
  git commit -m "feat: secure mission control sessions"
  git push origin feat/paper-platform-baseline
  ```

## Task 4: Protect Every API, Export, WebSocket, and Browser Boundary

**Files:**

- Modify: `maais/api/app.py`
- Create: `maais/api/headers.py`
- Test: `tests/integration/test_mission_control_surface_security.py`
- Test: `tests/unit/api/test_security_headers.py`

**Consumes:** Auth/CSRF dependencies and all existing routes.

**Produces:** Deny-by-default route protection, protected WebSocket/export surfaces, no-store policy, and strict browser headers.

- [ ] Enumerate every registered route in a test and require an explicit public/private classification. The public set is exactly `/healthz/live`, `/healthz/ready`, `/monitor/v1/health`, `/api/v1/auth/login`, `/api/v1/auth/session`, `/docs` only outside production, `/openapi.json` only outside production, and static login assets.

  ```python
  PUBLIC_PRODUCTION_PATHS = {
      "/healthz/live",
      "/healthz/ready",
      "/monitor/v1/health",
      "/api/v1/auth/login",
      "/api/v1/auth/session",
  }
  ```

- [ ] Write table-driven tests that call every GET/POST/export route unauthenticated and expect `401`/`403`, then authenticated and expect its domain response. Test WebSocket policy close before and after login.

- [ ] Write header tests for HSTS in production, CSP, `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, and `Cache-Control` behavior.

- [ ] Run tests and confirm existing public query/export/WebSocket routes fail the new expectations.

  ```bash
  uv run pytest -q tests/integration/test_mission_control_surface_security.py tests/unit/api/test_security_headers.py
  ```

- [ ] Refactor route registration into protected routers or apply a single session dependency at the `/api/v1` router and override only login/session-discovery endpoints. `/api/v1/auth/csrf` is session-authenticated and Origin/Host-validated but is the sole CSRF-bootstrap exception. Avoid ad hoc per-route omissions.

- [ ] Require CSRF on operator commands and logout; preserve idempotency and operator confirmation checks. Set the command actor from `OperatorPrincipal`, never from request JSON.

- [ ] Authenticate WebSocket cookies with the same repository and clock before `accept()`. Re-check expiry on each polling iteration and close `1008` when the session expires.

- [ ] Remove permissive production CORS. Local development keeps only `127.0.0.1:5173` and `localhost:5173`; production same-origin requires no cross-origin policy.

- [ ] Re-run surface tests, command tests, and API query/export regressions.

  ```bash
  uv run pytest -q tests/unit/api tests/integration/test_mission_control_surface_security.py tests/integration/test_mission_control_api.py tests/integration/test_mission_control_commands_api.py
  uv run ruff check maais/api tests/unit/api tests/integration/test_mission_control_surface_security.py
  uv run pyright maais/api
  ```

  Expected: all commands exit `0`; there is no unclassified production route.

- [ ] Commit.

  ```bash
  git add maais/api/app.py maais/api/headers.py tests/integration/test_mission_control_surface_security.py tests/unit/api/test_security_headers.py
  git commit -m "feat: protect mission control surfaces"
  git push origin feat/paper-platform-baseline
  ```

## Task 5: Replace Browser Token Storage With a Login Session

**Files:**

- Create: `dashboard/src/Login.tsx`
- Create: `dashboard/src/auth.ts`
- Modify: `dashboard/src/api.ts`
- Modify: `dashboard/src/App.tsx`
- Modify: `dashboard/src/OperatorConsole.tsx`
- Modify: `dashboard/src/types.ts`
- Modify: `dashboard/src/styles.css`
- Test: `dashboard/src/auth.test.ts`
- Test: `dashboard/src/api.test.ts`
- Test: `dashboard/src/App.test.tsx`
- Test: `dashboard/src/OperatorConsole.test.tsx`

**Consumes:** Auth endpoints, session cookie, CSRF response, existing command/query/event clients.

**Produces:** Password login, in-memory CSRF state, cookie-authenticated requests/WebSocket, and explicit expired-session handling.

- [ ] Write failing tests proving no auth token is read from or written to `localStorage`/`sessionStorage`, all fetches use same-origin credentials, commands include CSRF but no `Authorization`, and a `401` returns to login without losing server-side evidence.

  ```typescript
  expect(window.sessionStorage.getItem("maais-control-token")).toBeNull();
  expect(fetch).toHaveBeenCalledWith(
    "/api/v1/experiments",
    expect.objectContaining({ credentials: "same-origin" }),
  );
  ```

- [ ] Run frontend tests and confirm they fail against token-based API/App code.

  ```bash
  npm --prefix dashboard test -- auth.test.ts api.test.ts App.test.tsx OperatorConsole.test.tsx
  ```

- [ ] Implement `AuthState = checking | anonymous | authenticated`, keep the CSRF token only in React memory, re-fetch `/auth/session` on page reload, then call authenticated `/auth/csrf` to obtain a fresh in-memory token.

- [ ] Change API helpers to:

  ```typescript
  async function requestJson<T>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> {
    const response = await fetch(`${API_ROOT}${path}`, {
      ...init,
      credentials: "same-origin",
      headers: { Accept: "application/json", ...init.headers },
    });
    if (response.status === 401) throw new SessionExpiredError();
    if (!response.ok) throw await apiError(response);
    return response.json() as Promise<T>;
  }
  ```

- [ ] Remove the token input and storage from `App`/`OperatorConsole`; command calls accept `csrfToken` and send `X-CSRF-Token`.

- [ ] Keep export anchors same-origin so the secure cookie authenticates downloads. Do not place the CSRF token in URLs.

- [ ] Re-run frontend tests, typecheck, and build.

  ```bash
  npm --prefix dashboard test
  npm --prefix dashboard run typecheck
  npm --prefix dashboard run build
  ```

  Expected: all commands exit `0`; no storage-backed control token string remains.

- [ ] Commit.

  ```bash
  git add dashboard/src/Login.tsx dashboard/src/auth.ts dashboard/src/api.ts dashboard/src/App.tsx dashboard/src/OperatorConsole.tsx dashboard/src/types.ts dashboard/src/styles.css dashboard/src/auth.test.ts dashboard/src/api.test.ts dashboard/src/App.test.tsx dashboard/src/OperatorConsole.test.tsx
  git commit -m "feat: use secure mission control login"
  git push origin feat/paper-platform-baseline
  ```

## Task 6: Add Browser Security Smoke Tests

**Files:**

- Create: `tests/e2e/test_mission_control_auth.py`
- Modify: `scripts/browser-smoke.sh`
- Modify: `.github/workflows/ci.yml`

**Consumes:** Production dashboard build, FastAPI test server, PostgreSQL session repository.

**Produces:** Browser proof of unauthenticated denial, login, reload, command CSRF, logout, expired session, WebSocket protection, and export protection.

- [ ] Write browser tests that begin with a clean context, prove a direct experiment/export URL redirects to login or returns `401`, log in, reload, observe WebSocket updates, queue a harmless test command, log out, and prove back-navigation cannot display cached evidence.

- [ ] Add a test that modifies the CSRF header and expects `403` while the session remains valid.

- [ ] Run the browser suite locally against a disposable test database.

  ```bash
  uv run pytest -q tests/e2e/test_mission_control_auth.py
  ```

  Expected: all scenarios pass without exposing token/password values in screenshots, traces, or console output.

- [ ] Update CI browser smoke to set only test-scoped generated secrets, never production values, and upload no auth-bearing trace on success.

- [ ] Run the complete frontend and API security regression set.

  ```bash
  uv run pytest -q tests/unit/api tests/integration/test_mission_control_auth_api.py tests/integration/test_mission_control_surface_security.py tests/e2e/test_mission_control_auth.py
  npm --prefix dashboard test
  npm --prefix dashboard run typecheck
  npm --prefix dashboard run build
  ```

  Expected: all commands exit `0`.

- [ ] Commit.

  ```bash
  git add tests/e2e/test_mission_control_auth.py scripts/browser-smoke.sh .github/workflows/ci.yml
  git commit -m "test: prove mission control auth boundary"
  git push origin feat/paper-platform-baseline
  ```

## Definition of Done

- Railway rejects local bearer mode and every non-public API/export/WebSocket requires a valid server-side session.
- CSRF is enforced for all state-changing browser requests and is never placed in durable browser storage or URLs.
- Session and CSRF raw values never enter PostgreSQL, logs, Sentry, error details, test artifacts, or source.
- Login throttling is durable and PII-free; invalid credentials have uniform public behavior.
- Production security/caching headers are present and production CORS is same-origin only.
- Migration `0021` is reversible and its model/schema/repository tests pass.
- Local token-file Mission Control remains functional and the web service still only queues audited commands.
