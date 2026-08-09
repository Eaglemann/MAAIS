// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BrowserErrorBoundary,
  initializeBrowserObservability,
  redactBrowserBreadcrumb,
  redactBrowserEvent,
  resetBrowserObservabilityForTests,
  type BrowserObservabilityClient,
} from "./observability";

const CANARIES = [
  "postgresql://operator:db-secret@example.invalid/maais", // pragma: allowlist secret
  "Bearer auth-secret",
  "csrf-secret",
  "sentry-auth-secret",
  "AKIAEXAMPLESECRET",
  "telegram-secret",
  "opaque-query-value",
  "opaque-cookie-value",
  "203.0.113.7",
] as const;

afterEach(() => {
  cleanup();
  resetBrowserObservabilityForTests();
  vi.restoreAllMocks();
});

describe("browser Sentry privacy boundary", () => {
  it("removes every forbidden browser and trading value from the complete event", () => {
    const event = redactBrowserEvent({
      message: `request failed: ${CANARIES[0]}`,
      request: {
        url: `https://example.invalid/api?access_token=${CANARIES[6]}`,
        headers: { Authorization: CANARIES[1] },
        cookies: `session=${CANARIES[7]}`,
        data: { requestBody: CANARIES[3] },
      },
      user: { id: CANARIES[2], ip_address: CANARIES[8] },
      tags: { sentry_token: CANARIES[3], safe: "visible" },
      breadcrumbs: [
        {
          category: "fetch",
          message: CANARIES[5],
          data: {
            url: `https://example.invalid/api?token=${CANARIES[6]}`,
            response_body: CANARIES[4],
          },
        },
      ],
      contexts: {
        browser: { user_agent: CANARIES[3] },
        component: { componentStack: `Widget ${CANARIES[5]}` },
        trading: {
          account_equity: "10000",
          positions: [CANARIES[4]],
          order_quantity: "1.25",
        },
      },
      extra: {
        localStorage: { auth: CANARIES[1] },
        sessionStorage: { csrf: CANARIES[2] },
        raw_response_body: CANARIES[4],
      },
      exception: { values: [{ type: "Error", value: CANARIES[5] }] },
    });
    const serialized = JSON.stringify(event);

    for (const canary of CANARIES) expect(serialized).not.toContain(canary);
    expect(serialized).not.toContain("operator");
    expect(serialized).not.toContain("db-secret");
    expect(event).not.toHaveProperty("request");
    expect(event).not.toHaveProperty("user");
    expect(event.contexts).not.toHaveProperty("browser");
    expect(serialized).not.toContain("localStorage");
    expect(serialized).not.toContain("sessionStorage");
    expect(serialized).not.toContain("?");
    expect(serialized).toContain("visible");
  });

  it("redacts breadcrumb URLs, headers, bodies, cookies, and component metadata", () => {
    const breadcrumb = redactBrowserBreadcrumb({
      category: "fetch",
      message: `component failed ${CANARIES[2]}`,
      data: {
        url: `https://example.invalid/path?token=${CANARIES[6]}#fragment`,
        authorization: CANARIES[1],
        cookie: CANARIES[7],
        responseBody: CANARIES[5],
        componentStack: CANARIES[3],
      },
    });
    const serialized = JSON.stringify(breadcrumb);

    for (const canary of CANARIES) expect(serialized).not.toContain(canary);
    expect(serialized).toContain("https://example.invalid/path");
    expect(serialized).not.toContain("?");
    expect(serialized).not.toContain("#fragment");
  });

  it("initializes exactly once with every collection and sampling surface disabled", () => {
    const init = vi.fn();
    const client: BrowserObservabilityClient = { init, captureException: vi.fn() };
    const config = {
      dsn: "https://public@example.invalid/1",
      environment: "qualification",
      release: "a".repeat(40),
    };

    expect(initializeBrowserObservability(config, client)).toBe(true);
    expect(initializeBrowserObservability(config, client)).toBe(true);
    expect(init).toHaveBeenCalledTimes(1);
    const options = init.mock.calls[0]?.[0];
    expect(options).toMatchObject({
      dsn: config.dsn,
      environment: "qualification",
      release: "a".repeat(40),
      sendDefaultPii: false,
      sampleRate: 1,
      tracesSampleRate: 0,
      profilesSampleRate: 0,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
      enableLogs: false,
      enableMetrics: false,
      sendClientReports: false,
      tracePropagationTargets: [],
      enhanceFetchErrorMessages: false,
    });
    expect(options?.dataCollection).toEqual({
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
    });
    const defaults = [
      { name: "Replay" },
      { name: "BrowserSession" },
      { name: "HttpContext" },
      { name: "Breadcrumbs" },
      { name: "GlobalHandlers" },
    ];
    const integrations = options?.integrations;
    expect(typeof integrations).toBe("function");
    const selected = typeof integrations === "function" ? integrations(defaults) : [];
    expect(selected.map((integration: { name: string }) => integration.name)).toEqual([
      "GlobalHandlers",
      "Breadcrumbs",
    ]);
  });

  it("stays disabled unless the complete public release identity is valid", () => {
    const init = vi.fn();
    const client: BrowserObservabilityClient = { init, captureException: vi.fn() };

    expect(initializeBrowserObservability({}, client)).toBe(false);
    expect(initializeBrowserObservability({
      dsn: "https://public@example.invalid/1",
      environment: "qualification",
      release: "short",
    }, client)).toBe(false);
    expect(init).not.toHaveBeenCalled();
  });
});

describe("Mission Control browser error boundary", () => {
  it("shows only an operator-safe correlation code and captures the exception", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const capture = vi.fn(() => "event-id");
    const failure = new Error(`private failure ${CANARIES[5]}`);
    function Broken(): ReactNode {
      throw failure;
    }

    render(createElement(
      BrowserErrorBoundary,
      { captureException: capture },
      createElement(Broken),
    ));

    expect(screen.getByRole("alert")).toHaveTextContent("Mission Control encountered an error");
    expect(screen.getByText(/Reference MC-[A-F0-9]{8}/)).toBeInTheDocument();
    expect(screen.queryByText(/private failure/)).not.toBeInTheDocument();
    expect(capture).toHaveBeenCalledWith(
      failure,
      expect.objectContaining({
        componentStack: expect.any(String),
        correlationCode: expect.stringMatching(/^MC-[A-F0-9]{8}$/),
      }),
    );
  });
});
