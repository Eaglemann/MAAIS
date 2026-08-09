// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  loginOperator,
  logoutOperator,
  restoreOperatorSession,
} from "./auth";

const localStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
};
const sessionStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
};

beforeEach(() => {
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: localStorageMock,
  });
  Object.defineProperty(window, "sessionStorage", {
    configurable: true,
    value: sessionStorageMock,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function response(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("operator browser session", () => {
  it("treats expiry during CSRF rotation as an anonymous session", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({
        authenticated: true,
        actor: "sole_operator",
        auth_mode: "operator_session",
        expires_at: "2026-08-09T18:00:00Z",
      }))
      .mockResolvedValueOnce(response(null, 401));
    vi.stubGlobal("fetch", fetchMock);

    await expect(restoreOperatorSession()).resolves.toEqual({
      status: "anonymous",
      reason: "expired",
    });
  });

  it("restores the cookie session and rotates CSRF only into memory", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({
        authenticated: true,
        actor: "sole_operator",
        auth_mode: "operator_session",
        expires_at: "2026-08-09T18:00:00Z",
      }))
      .mockResolvedValueOnce(response({ csrf_token: "memory-only-csrf" }));
    vi.stubGlobal("fetch", fetchMock);

    const state = await restoreOperatorSession();

    expect(state).toEqual({
      status: "authenticated",
      actor: "sole_operator",
      authMode: "operator_session",
      expiresAt: "2026-08-09T18:00:00Z",
      csrfToken: "memory-only-csrf",
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/auth/session",
      expect.objectContaining({ credentials: "same-origin" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/auth/csrf",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    expect(localStorageMock.getItem).not.toHaveBeenCalled();
    expect(localStorageMock.setItem).not.toHaveBeenCalled();
    expect(sessionStorageMock.getItem).not.toHaveBeenCalled();
    expect(sessionStorageMock.setItem).not.toHaveBeenCalled();
  });

  it("logs in and out without persisting the password or CSRF token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({
        authenticated: true,
        actor: "sole_operator",
        auth_mode: "operator_session",
        csrf_token: "login-csrf",
        expires_at: "2026-08-09T18:00:00Z",
      }))
      .mockResolvedValueOnce(response(null, 204));
    vi.stubGlobal("fetch", fetchMock);

    const state = await loginOperator("correct horse battery staple");
    await logoutOperator("login-csrf");

    expect(state.status).toBe("authenticated");
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/auth/login",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({ password: "correct horse battery staple" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/auth/logout",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: expect.objectContaining({ "X-CSRF-Token": "login-csrf" }),
      }),
    );
    expect(localStorageMock.getItem).not.toHaveBeenCalled();
    expect(localStorageMock.setItem).not.toHaveBeenCalled();
    expect(sessionStorageMock.getItem).not.toHaveBeenCalled();
    expect(sessionStorageMock.setItem).not.toHaveBeenCalled();
  });
});
