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
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeWebSocket.instances = [];
});

describe("resumable Mission Control event feed", () => {
  it("catches up before connecting and reconnects from the last durable cursor", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = new URL(String(input), window.location.href);
      const cursor = Number(url.searchParams.get("after_cursor") ?? 0);
      return {
        ok: true,
        json: async () => ({
          items: [],
          limit: 500,
          has_more: false,
          next_cursor: cursor,
        }),
      } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onEvents = vi.fn();
    const onCursor = vi.fn();
    const onStatus = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: 11,
      onEvents,
      onCursor,
      onStatus,
      reconnectDelayMs: 1_000,
    });
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/events?after_cursor=11"),
      expect.any(Object),
    );
    expect(FakeWebSocket.instances).toHaveLength(1);
    const firstSocket = FakeWebSocket.instances[0]!;
    expect(firstSocket.url).toContain(
      "/api/v1/events/stream?after_cursor=11",
    );
    firstSocket.open();
    firstSocket.emit({
      type: "events",
      items: [
        { cursor: 12, event_type: "decision.completed", created_at: "2026-08-02T12:00:00Z", payload: {} },
        { cursor: 13, event_type: "operator_command.requested", created_at: "2026-08-02T12:00:01Z", payload: {} },
      ],
      limit: 500,
      has_more: false,
      next_cursor: 13,
    });

    expect(onEvents).toHaveBeenCalledTimes(1);
    expect(onCursor).toHaveBeenLastCalledWith(13);
    firstSocket.disconnect();
    await vi.advanceTimersByTimeAsync(1_000);
    await flushPromises();

    expect(fetchMock).toHaveBeenLastCalledWith(
      expect.stringContaining("/events?after_cursor=13"),
      expect.any(Object),
    );
    expect(FakeWebSocket.instances).toHaveLength(2);
    const secondSocket = FakeWebSocket.instances[1]!;
    expect(secondSocket.url).toContain("after_cursor=13");

    stop();
    expect(secondSocket.closed).toBe(true);
    expect(onStatus).toHaveBeenLastCalledWith("stopped");
  });

  it("recovers when a restored database has a lower outbox high-water mark", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        items: [],
        limit: 500,
        has_more: false,
        next_cursor: 4,
      }),
    }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const onCursor = vi.fn();

    const stop = startResumableEventFeed({
      initialCursor: 99,
      onEvents: () => undefined,
      onCursor,
      onStatus: () => undefined,
    });
    await flushPromises();

    expect(onCursor).toHaveBeenLastCalledWith(4);
    expect(FakeWebSocket.instances[0]!.url).toContain("after_cursor=4");
    stop();
  });
});
