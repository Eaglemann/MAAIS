import { requestJson, requestVoid, SessionExpiredError } from "./api";
import type {
  AuthMode,
  AuthSessionView,
  CsrfTokenResponse,
  LoginResponse,
} from "./types";

export type AuthState =
  | { status: "checking" }
  | { status: "anonymous"; reason: "required" | "expired" | "signed_out" }
  | {
      status: "authenticated";
      actor: string;
      authMode: AuthMode;
      expiresAt: string | null;
      csrfToken: string | null;
    };

export async function restoreOperatorSession(signal?: AbortSignal): Promise<AuthState> {
  let session: AuthSessionView;
  try {
    session = await requestJson<AuthSessionView>("/auth/session", { signal });
  } catch (reason: unknown) {
    if (reason instanceof SessionExpiredError) {
      return { status: "anonymous", reason: "expired" };
    }
    throw reason;
  }
  if (session.auth_mode === "local_token") {
    return {
      status: "authenticated",
      actor: "local_operator",
      authMode: "local_token",
      expiresAt: null,
      csrfToken: null,
    };
  }
  if (!session.authenticated || session.actor === null) {
    return { status: "anonymous", reason: "required" };
  }
  let csrf: CsrfTokenResponse;
  try {
    csrf = await requestJson<CsrfTokenResponse>("/auth/csrf", {
      method: "POST",
      signal,
    });
  } catch (reason: unknown) {
    if (reason instanceof SessionExpiredError) {
      return { status: "anonymous", reason: "expired" };
    }
    throw reason;
  }
  return {
    status: "authenticated",
    actor: session.actor,
    authMode: session.auth_mode,
    expiresAt: session.expires_at,
    csrfToken: csrf.csrf_token,
  };
}

export async function loginOperator(
  password: string,
  signal?: AbortSignal,
): Promise<Extract<AuthState, { status: "authenticated" }>> {
  let login: LoginResponse;
  try {
    login = await requestJson<LoginResponse>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
      signal,
    });
  } catch (reason: unknown) {
    if (reason instanceof SessionExpiredError) {
      throw new Error("Invalid operator credentials or login temporarily locked");
    }
    throw reason;
  }
  return {
    status: "authenticated",
    actor: login.actor,
    authMode: login.auth_mode,
    expiresAt: login.expires_at,
    csrfToken: login.csrf_token,
  };
}

export function logoutOperator(csrfToken: string, signal?: AbortSignal): Promise<void> {
  return requestVoid("/auth/logout", {
    method: "POST",
    headers: { "X-CSRF-Token": csrfToken },
    signal,
  });
}
