import * as Sentry from "@sentry/react";
import { Component, createElement, type ErrorInfo, type ReactNode } from "react";

const REDACTED = "[REDACTED]";
const TRUNCATED = "[TRUNCATED]";
const MAX_DEPTH = 10;
const MAX_ITEMS = 100;
const MAX_STRING_LENGTH = 8_192;
const RELEASE_PATTERN = /^[0-9a-f]{40}$/;
const ENVIRONMENTS = new Set(["qualification", "production"]);
const SAFE_URL_PROTOCOLS = new Set(["http:", "https:"]);
const ALLOWED_INTEGRATIONS = new Set([
  "InboundFilters",
  "FunctionToString",
  "BrowserApiErrors",
  "GlobalHandlers",
  "LinkedErrors",
  "Dedupe",
]);
const SENSITIVE_KEYS = new Set([
  "account_equity",
  "access_key",
  "api_key",
  "authorization",
  "balance",
  "body",
  "client_secret",
  "cookie",
  "credentials",
  "csrf",
  "database_url",
  "dsn",
  "headers",
  "ip_address",
  "local_storage",
  "object_store_credential",
  "order_quantity",
  "passphrase",
  "password",
  "position",
  "positions",
  "private_key",
  "quantity",
  "raw_operator_input",
  "raw_response",
  "request_body",
  "response_body",
  "secret",
  "session_storage",
  "session_token",
  "telegram_credential",
  "token",
  "user_agent",
  "vars",
]);
const OMITTED_KEYS = new Set(["local_storage", "session_storage"]);
const URL_TEXT = /\b(?:https?|postgres(?:ql)?|redis|rediss|amqp|amqps):\/\/[^\s"'<>]+/gi;
const BEARER = /\bbearer\s+[^\s,;]+/gi;
const AWS_ACCESS_KEY = /\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b/g;
const COOKIE_VALUE = /\b((?:auth|cookie|csrf|session|token)=)[^;\s]+/gi;
const IPV4_ADDRESS = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g;
const SECRET_TOKEN = /\b[a-z0-9_-]*(?:csrf|sentry|telegram|secret|token|password|api[_-]?key)[a-z0-9_-]*\b/gi;

type UnknownRecord = Record<string, unknown>;

export interface BrowserObservabilityConfig {
  dsn?: string;
  environment?: string;
  release?: string;
}

export interface BrowserObservabilityClient {
  init(options: Sentry.BrowserOptions): unknown;
  captureException(exception: unknown, hint?: Parameters<typeof Sentry.captureException>[1]): string;
}

export interface BrowserErrorMetadata {
  componentStack: string;
  correlationCode: string;
}

interface BrowserErrorBoundaryProps {
  children?: ReactNode;
  captureException?: (error: unknown, metadata: BrowserErrorMetadata) => string | undefined;
}

interface BrowserErrorBoundaryState {
  failed: boolean;
}

const defaultClient: BrowserObservabilityClient = {
  init: Sentry.init,
  captureException: Sentry.captureException,
};
let initializedFingerprint: string | null = null;
let activeClient: BrowserObservabilityClient = defaultClient;

export function initializeBrowserObservability(
  config: BrowserObservabilityConfig = browserObservabilityConfig(),
  client: BrowserObservabilityClient = defaultClient,
): boolean {
  const resolved = validConfig(config);
  if (resolved === null) return false;
  const fingerprint = `${resolved.dsn}\u0000${resolved.environment}\u0000${resolved.release}`;
  if (initializedFingerprint !== null) return initializedFingerprint === fingerprint;
  try {
    client.init({
      dsn: resolved.dsn,
      environment: resolved.environment,
      release: resolved.release,
      sendDefaultPii: false,
      dataCollection: {
        userInfo: false,
        cookies: false,
        httpHeaders: { request: false, response: false },
        httpBodies: [],
        urlQueryParams: false,
        graphQL: { document: false, variables: false },
        genAI: { inputs: false, outputs: false },
        databaseQueryData: false,
        stackFrameVariables: false,
        frameContextLines: 0,
      },
      sampleRate: 1,
      tracesSampleRate: 0,
      profilesSampleRate: 0,
      profileSessionSampleRate: 0,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
      enableLogs: false,
      enableMetrics: false,
      sendClientReports: false,
      tracePropagationTargets: [],
      enhanceFetchErrorMessages: false,
      maxBreadcrumbs: 50,
      normalizeDepth: MAX_DEPTH,
      initialScope: {
        tags: {
          "maais.service_role": "web",
          "maais.deployment_target": "railway",
        },
      },
      integrations(defaultIntegrations) {
        return [
          ...defaultIntegrations.filter((integration) =>
            ALLOWED_INTEGRATIONS.has(integration.name)),
          Sentry.breadcrumbsIntegration({
            console: false,
            dom: false,
            fetch: true,
            history: false,
            sentry: true,
            xhr: true,
          }),
        ];
      },
      beforeSend(event) {
        return redactBrowserEvent(event) as unknown as Sentry.ErrorEvent;
      },
      beforeBreadcrumb(breadcrumb) {
        return redactBrowserBreadcrumb(breadcrumb) as Sentry.Breadcrumb;
      },
    });
  } catch {
    return false;
  }
  activeClient = client;
  initializedFingerprint = fingerprint;
  return true;
}

export function captureBrowserException(
  exception: unknown,
  metadata: BrowserErrorMetadata,
): string | undefined {
  if (initializedFingerprint === null) return undefined;
  try {
    return activeClient.captureException(exception, {
      tags: {
        "maais.event": "mission_control_render_failure",
        "maais.error_code": "browser_render_exception",
        "maais.correlation_code": metadata.correlationCode,
      },
      contexts: {
        maais: {
          correlation_code: metadata.correlationCode,
          component_stack: metadata.componentStack,
        },
      },
    });
  } catch {
    return undefined;
  }
}

export function redactBrowserEvent<T extends object>(event: T): T {
  const sanitized: UnknownRecord = { ...(event as UnknownRecord) };
  delete sanitized.request;
  delete sanitized.user;
  delete sanitized.server_name;
  if (isRecord(sanitized.contexts)) {
    const contexts = { ...sanitized.contexts };
    for (const key of ["browser", "culture", "device", "os"]) delete contexts[key];
    sanitized.contexts = contexts;
  }
  return redactValue(sanitized, null, 0, new WeakSet()) as T;
}

export function redactBrowserBreadcrumb<T extends object>(breadcrumb: T): T {
  return redactValue(
    { ...(breadcrumb as UnknownRecord) },
    null,
    0,
    new WeakSet(),
  ) as T;
}

export class BrowserErrorBoundary extends Component<
  BrowserErrorBoundaryProps,
  BrowserErrorBoundaryState
> {
  readonly correlationCode = correlationCode();
  state: BrowserErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): BrowserErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    const capture = this.props.captureException ?? captureBrowserException;
    capture(error, {
      componentStack: info.componentStack ?? "unavailable",
      correlationCode: this.correlationCode,
    });
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return createElement(
      "main",
      { className: "boot-state", role: "alert" },
      createElement("span", { className: "brand-mark" }, "M"),
      createElement("h1", null, "Mission Control encountered an error"),
      createElement(
        "p",
        null,
        "Do not infer trading state from this browser failure. Verify system health before any operator action.",
      ),
      createElement("strong", null, `Reference ${this.correlationCode}`),
      createElement(
        "button",
        { type: "button", onClick: () => window.location.reload() },
        "Reload Mission Control",
      ),
    );
  }
}

export function resetBrowserObservabilityForTests(): void {
  initializedFingerprint = null;
  activeClient = defaultClient;
}

function browserObservabilityConfig(): BrowserObservabilityConfig {
  return {
    dsn: import.meta.env.VITE_SENTRY_DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT,
    release: import.meta.env.VITE_SENTRY_RELEASE,
  };
}

function validConfig(config: BrowserObservabilityConfig): Required<BrowserObservabilityConfig> | null {
  const dsn = config.dsn?.trim() ?? "";
  const environment = config.environment?.trim() ?? "";
  const release = config.release?.trim() ?? "";
  if (!canonicalDsn(dsn) || !ENVIRONMENTS.has(environment) || !RELEASE_PATTERN.test(release)) {
    return null;
  }
  return { dsn, environment, release };
}

function canonicalDsn(value: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:"
      && parsed.username.length > 0
      && parsed.password.length === 0
      && parsed.pathname.replaceAll("/", "").length > 0
      && parsed.search.length === 0
      && parsed.hash.length === 0;
  } catch {
    return false;
  }
}

function redactValue(
  value: unknown,
  key: string | null,
  depth: number,
  seen: WeakSet<object>,
): unknown {
  const normalizedKey = key === null ? "" : normalizeKey(key);
  if (normalizedKey && isSensitiveKey(normalizedKey)) return REDACTED;
  if (depth >= MAX_DEPTH) return TRUNCATED;
  if (typeof value === "string") {
    return key !== null && normalizeKey(key) === "url" ? safeUrl(value) : redactText(value);
  }
  if (value === null || ["boolean", "number"].includes(typeof value)) return value;
  if (Array.isArray(value)) {
    if (seen.has(value)) return [TRUNCATED];
    seen.add(value);
    const items = value.slice(0, MAX_ITEMS).map((item) =>
      redactValue(item, null, depth + 1, seen));
    if (value.length > MAX_ITEMS) items.push(TRUNCATED);
    seen.delete(value);
    return items;
  }
  if (isRecord(value)) {
    if (seen.has(value)) return { cycle: TRUNCATED };
    seen.add(value);
    const result: UnknownRecord = {};
    for (const [childKey, childValue] of Object.entries(value).slice(0, MAX_ITEMS)) {
      const normalized = normalizeKey(childKey);
      if (OMITTED_KEYS.has(normalized)) continue;
      result[childKey] = redactValue(childValue, childKey, depth + 1, seen);
    }
    seen.delete(value);
    return result;
  }
  return redactText(String(value));
}

function normalizeKey(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

function isSensitiveKey(normalized: string): boolean {
  const padded = `_${normalized}_`;
  return [...SENSITIVE_KEYS].some((marker) => padded.includes(`_${marker}_`));
}

function redactText(input: string): string {
  const redacted = redactNonUrlText(input.replace(URL_TEXT, (value) => safeUrl(value)));
  return redacted.length <= MAX_STRING_LENGTH
    ? redacted
    : `${redacted.slice(0, MAX_STRING_LENGTH - 3)}...`;
}

function redactNonUrlText(input: string): string {
  return input
    .replace(BEARER, REDACTED)
    .replace(AWS_ACCESS_KEY, REDACTED)
    .replace(COOKIE_VALUE, `$1${REDACTED}`)
    .replace(IPV4_ADDRESS, REDACTED)
    .replace(SECRET_TOKEN, REDACTED);
}

function safeUrl(value: string): string {
  try {
    const absolute = /^[a-z][a-z0-9+.-]*:\/\//i.test(value);
    const parsed = new URL(value, "https://mission-control.invalid");
    if (!SAFE_URL_PROTOCOLS.has(parsed.protocol)) return REDACTED;
    const path = parsed.pathname || "/";
    return redactNonUrlText(absolute ? `${parsed.protocol}//${parsed.host}${path}` : path);
  } catch {
    return REDACTED;
  }
}

function correlationCode(): string {
  const bytes = new Uint8Array(4);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
  else {
    const fallback = Date.now();
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = (fallback >>> (index * 8)) & 0xff;
    }
  }
  return `MC-${[...bytes].map((value) => value.toString(16).padStart(2, "0")).join("").toUpperCase()}`;
}

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
