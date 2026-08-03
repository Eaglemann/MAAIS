import type {
  DecisionDetail,
  DecisionFilters,
  DecisionPage,
  ExperimentListItem,
  ExperimentOverview,
  EventFeedStatus,
  OperatorActionDraft,
  OperatorCommand,
  OperatorCommandPage,
  OutboxCursorEvent,
  OutboxCursorPage,
  ResearchLabView,
  TradePage,
} from "./types";

const API_ROOT = import.meta.env.VITE_API_ROOT ?? "/api/v1";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The HTTP status remains the authoritative fallback.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

async function postJson<T>(
  path: string,
  token: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    cache: "no-store",
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The HTTP status remains the authoritative fallback.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function listExperiments(signal?: AbortSignal): Promise<ExperimentListItem[]> {
  return getJson<ExperimentListItem[]>("/experiments", signal);
}

export function getOverview(
  experimentId: string,
  signal?: AbortSignal,
): Promise<ExperimentOverview> {
  return getJson<ExperimentOverview>(`/experiments/${experimentId}/overview`, signal);
}

export function listDecisions(
  experimentId: string,
  filters: DecisionFilters,
  signal?: AbortSignal,
): Promise<DecisionPage> {
  const params = new URLSearchParams({ limit: "200" });
  if (filters.symbol.trim()) params.set("symbol", filters.symbol.trim().toUpperCase());
  if (filters.status) params.set("status", filters.status);
  if (filters.disposition) params.set("disposition", filters.disposition);
  if (filters.reasonCode.trim()) params.set("reason_code", filters.reasonCode.trim());
  return getJson<DecisionPage>(
    `/experiments/${experimentId}/decisions?${params.toString()}`,
    signal,
  );
}

export function listTrades(
  experimentId: string,
  symbol: string,
  signal?: AbortSignal,
): Promise<TradePage> {
  const params = new URLSearchParams({ limit: "200" });
  if (symbol.trim()) params.set("symbol", symbol.trim().toUpperCase());
  return getJson<TradePage>(
    `/experiments/${experimentId}/trades?${params.toString()}`,
    signal,
  );
}

export function getDecision(
  decisionId: string,
  signal?: AbortSignal,
): Promise<DecisionDetail> {
  return getJson<DecisionDetail>(`/decisions/${decisionId}`, signal);
}

export function getResearch(
  experimentId: string,
  signal?: AbortSignal,
): Promise<ResearchLabView> {
  return getJson<ResearchLabView>(`/experiments/${experimentId}/research`, signal);
}

export function listCommands(
  experimentId: string,
  signal?: AbortSignal,
): Promise<OperatorCommandPage> {
  return getJson<OperatorCommandPage>(`/experiments/${experimentId}/commands`, signal);
}

export function requestOperatorCommand(
  experimentId: string,
  token: string,
  idempotencyKey: string,
  draft: OperatorActionDraft,
  signal?: AbortSignal,
): Promise<OperatorCommand> {
  return postJson<OperatorCommand>(
    `/experiments/${experimentId}/commands`,
    token,
    {
      command_type: draft.commandType,
      idempotency_key: idempotencyKey,
      reason: draft.reason,
      payload: draft.payload,
      confirmation: draft.confirmation,
    },
    signal,
  );
}

export function getEventPage(
  afterCursor: number,
  signal?: AbortSignal,
): Promise<OutboxCursorPage> {
  const params = new URLSearchParams({
    after_cursor: String(afterCursor),
    limit: "500",
  });
  return getJson<OutboxCursorPage>(`/events?${params.toString()}`, signal);
}

function eventStreamUrl(afterCursor: number): string {
  const base = typeof window === "undefined" ? "http://127.0.0.1" : window.location.href;
  const url = new URL(`${API_ROOT}/events/stream`, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after_cursor", String(afterCursor));
  return url.toString();
}

export function startResumableEventFeed({
  initialCursor,
  onEvents,
  onCursor,
  onStatus,
  reconnectDelayMs = 1_500,
}: {
  initialCursor: number;
  onEvents: () => void;
  onCursor: (cursor: number) => void;
  onStatus: (status: EventFeedStatus) => void;
  reconnectDelayMs?: number;
}): () => void {
  let cursor = Math.max(0, Math.trunc(initialCursor));
  let stopped = false;
  let reconnectTimer: number | null = null;
  let invalidationTimer: number | null = null;
  let socket: WebSocket | null = null;
  const controller = new AbortController();

  function acceptPage(page: OutboxCursorPage): boolean {
    if (page.items.length === 0 && page.next_cursor < cursor) {
      cursor = page.next_cursor;
      onCursor(cursor);
      return true;
    }
    const unseen = page.items
      .filter((event) => event.cursor > cursor)
      .sort((left, right) => left.cursor - right.cursor);
    const next = Math.max(
      cursor,
      page.next_cursor,
      unseen.at(-1)?.cursor ?? cursor,
    );
    if (next > cursor) {
      cursor = next;
      onCursor(cursor);
    }
    return unseen.length > 0;
  }

  function invalidateAfterBurst() {
    if (invalidationTimer !== null) window.clearTimeout(invalidationTimer);
    invalidationTimer = window.setTimeout(() => {
      invalidationTimer = null;
      onEvents();
    }, 2_000);
  }

  function scheduleReconnect() {
    if (stopped || reconnectTimer !== null) return;
    onStatus("reconnecting");
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      void resume();
    }, reconnectDelayMs);
  }

  async function resume() {
    if (stopped) return;
    onStatus("catching_up");
    try {
      let page: OutboxCursorPage;
      let snapshotInvalidated = false;
      do {
        page = await getEventPage(cursor, controller.signal);
        if (stopped) return;
        snapshotInvalidated = acceptPage(page) || snapshotInvalidated;
      } while (page.has_more);
      if (snapshotInvalidated) {
        if (invalidationTimer !== null) window.clearTimeout(invalidationTimer);
        invalidationTimer = null;
        onEvents();
      }

      const nextSocket = new WebSocket(eventStreamUrl(cursor));
      socket = nextSocket;
      nextSocket.onopen = () => {
        if (!stopped) onStatus("live");
      };
      nextSocket.onmessage = (message) => {
        if (stopped) return;
        try {
          const payload = JSON.parse(String(message.data)) as
            | ({ type: "events" } & OutboxCursorPage)
            | { type: "heartbeat"; next_cursor: number };
          if (payload.type === "events" && acceptPage(payload)) invalidateAfterBurst();
        } catch {
          nextSocket.close();
        }
      };
      nextSocket.onerror = () => nextSocket.close();
      nextSocket.onclose = () => {
        if (socket === nextSocket) socket = null;
        scheduleReconnect();
      };
    } catch (reason: unknown) {
      if (!controller.signal.aborted) scheduleReconnect();
    }
  }

  void resume();
  return () => {
    stopped = true;
    controller.abort();
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
    if (invalidationTimer !== null) window.clearTimeout(invalidationTimer);
    if (socket !== null) {
      socket.onclose = null;
      socket.close();
    }
    onStatus("stopped");
  };
}
