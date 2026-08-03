// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import { startResumableEventFeed } from "./api";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string | URL) {
    this.url = String(url);
    FakeWebSocket.instances.push(this);
  }

  open() {
    this.onopen?.();
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }

  disconnect() {
    this.onclose?.();
  }

  close() {
    this.closed = true;
  }
}

async function flushPromises() {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

function page(cursors: number[], nextCursor: number, hasMore = false) {
  return {
    items: cursors.map((cursor) => ({
      cursor,
      event_type: "decision.completed",
      created_at: "2026-08-02T12:00:00Z",
      payload: {},
    })),
    limit: 500,
    has_more: hasMore,
    next_cursor: nextCursor,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeWebSocket.instances = [];
});

describe("resumable Mission Control event feed", () => {
  it("fast-forwards a fresh dashboard to the current outbox head and requests one snapshot", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => page([], 42),
    }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onEvents = vi.fn();
    const onCursor = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: Number.MAX_SAFE_INTEGER,
      onEvents,
      onCursor,
      onStatus: () => undefined,
    });
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(`/events?after_cursor=${Number.MAX_SAFE_INTEGER}`),
      expect.any(Object),
    );
    expect(onCursor).toHaveBeenLastCalledWith(42);
    expect(onEvents).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances[0]!.url).toContain("after_cursor=42");
    stop();
  });

  it("coalesces all paged catch-up events into one snapshot invalidation", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const cursor = Number(
        new URL(String(input), window.location.href).searchParams.get("after_cursor") ?? 0,
      );
      const body = cursor === 11 ? page([12], 12, true) : page([13], 13);
      return { ok: true, json: async () => body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onEvents = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: 11,
      onEvents,
      onCursor: () => undefined,
      onStatus: () => undefined,
    });
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onEvents).toHaveBeenCalledTimes(1);
    expect(FakeWebSocket.instances[0]!.url).toContain("after_cursor=13");
    stop();
  });

  it("debounces a burst of live event pages into one snapshot invalidation", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => page([], 11),
    }) as Response));
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onEvents = vi.fn();
    const onCursor = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: 11,
      onEvents,
      onCursor,
      onStatus: () => undefined,
    });
    await flushPromises();
    const socket = FakeWebSocket.instances[0]!;
    socket.open();

    socket.emit({ type: "events", ...page([12], 12) });
    socket.emit({ type: "events", ...page([13], 13) });
    socket.emit({ type: "events", ...page([14], 14) });

    expect(onEvents).not.toHaveBeenCalled();
    expect(onCursor).toHaveBeenLastCalledWith(14);
    await vi.advanceTimersByTimeAsync(1_999);
    expect(onEvents).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(onEvents).toHaveBeenCalledTimes(1);

    stop();
    expect(socket.closed).toBe(true);
  });

  it("preserves a pending live invalidation across a reconnect", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const cursor = Number(
        new URL(String(input), window.location.href).searchParams.get("after_cursor") ?? 0,
      );
      return { ok: true, json: async () => page([], cursor) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onEvents = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: 11,
      onEvents,
      onCursor: () => undefined,
      onStatus: () => undefined,
      reconnectDelayMs: 1_000,
    });
    await flushPromises();
    const firstSocket = FakeWebSocket.instances[0]!;
    firstSocket.emit({ type: "events", ...page([12], 12) });
    firstSocket.disconnect();

    await vi.advanceTimersByTimeAsync(1_000);
    await flushPromises();
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(onEvents).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(999);
    expect(onEvents).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(onEvents).toHaveBeenCalledTimes(1);
    stop();
  });

  it("reconnects from the last durable cursor and stops cleanly", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const cursor = Number(
        new URL(String(input), window.location.href).searchParams.get("after_cursor") ?? 0,
      );
      return { ok: true, json: async () => page([], cursor) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onStatus = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: 11,
      onEvents: () => undefined,
      onCursor: () => undefined,
      onStatus,
      reconnectDelayMs: 1_000,
    });
    await flushPromises();
    const firstSocket = FakeWebSocket.instances[0]!;
    firstSocket.disconnect();
    await vi.advanceTimersByTimeAsync(1_000);
    await flushPromises();

    expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining("/events?after_cursor=11"),
      expect.any(Object),
    );
    expect(FakeWebSocket.instances).toHaveLength(2);
    const secondSocket = FakeWebSocket.instances[1]!;
    stop();
    expect(secondSocket.closed).toBe(true);
    expect(onStatus).toHaveBeenLastCalledWith("stopped");
  });
});
