// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  decisionCsvUrl,
  decisionJsonUrl,
  listDecisions,
  listTrades,
  tradeCsvUrl,
} from "./api";
import type { DecisionFilters, PageCursor, TradeFilters } from "./types";

const DECISION_FILTERS: DecisionFilters = {
  symbol: " btcusdt ",
  status: "completed",
  direction: "long",
  disposition: "approved",
  reasonCode: "accepted",
  fromAt: "2026-08-02T10:00:00.000Z",
  toAt: "2026-08-02T11:00:00.000Z",
  regime: "trending",
  strategyVersionId: "11111111-1111-4111-8111-111111111111",
  gateType: "ev",
  gatePassed: "true",
  agentName: "trend",
  agentDirection: "long",
  proposalStatus: "approved",
  orderStatus: "filled",
  outcome: "filled",
};

const TRADE_FILTERS: TradeFilters = {
  symbol: " btcusdt ",
  fromAt: "2026-08-02T10:00:00.000Z",
  toAt: "2026-08-02T11:00:00.000Z",
  direction: "long",
  regime: "trending",
  strategyVersionId: "11111111-1111-4111-8111-111111111111",
  proposalStatus: "approved",
  decisionDisposition: "approved",
  orderStatus: "filled",
  counterfactualStatus: "",
  outcome: "filled",
};

const CURSOR: PageCursor = {
  beforeAt: "2026-08-02T10:30:00.000Z",
  beforeId: "22222222-2222-4222-8222-222222222222",
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function stubPage() {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    json: async () => ({
      items: [],
      limit: 200,
      has_more: false,
      next_before_at: null,
      next_before_id: null,
    }),
  }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedUrl(fetchMock: ReturnType<typeof vi.fn>): URL {
  const path = String(fetchMock.mock.calls[0]?.[0]);
  return new URL(path, "http://mission-control.local");
}

describe("Mission Control audit query contract", () => {
  it("serializes every decision filter and both cursor values", async () => {
    const fetchMock = stubPage();

    await listDecisions("experiment-1", DECISION_FILTERS, CURSOR);

    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/experiments/experiment-1/decisions");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "200",
      symbol: "BTCUSDT",
      status: "completed",
      direction: "long",
      disposition: "approved",
      reason_code: "accepted",
      from_at: "2026-08-02T10:00:00.000Z",
      to_at: "2026-08-02T11:00:00.000Z",
      regime: "trending",
      strategy_version_id: "11111111-1111-4111-8111-111111111111",
      gate_type: "ev",
      gate_passed: "true",
      agent_name: "trend",
      agent_direction: "long",
      proposal_status: "approved",
      order_status: "filled",
      outcome: "filled",
      before_at: "2026-08-02T10:30:00.000Z",
      before_id: "22222222-2222-4222-8222-222222222222",
    });
  });

  it("serializes every trade filter and both cursor values", async () => {
    const fetchMock = stubPage();

    await listTrades("experiment-1", TRADE_FILTERS, CURSOR);

    const url = requestedUrl(fetchMock);
    expect(url.pathname).toBe("/api/v1/experiments/experiment-1/trades");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      limit: "200",
      symbol: "BTCUSDT",
      from_at: "2026-08-02T10:00:00.000Z",
      to_at: "2026-08-02T11:00:00.000Z",
      direction: "long",
      regime: "trending",
      strategy_version_id: "11111111-1111-4111-8111-111111111111",
      proposal_status: "approved",
      decision_disposition: "approved",
      order_status: "filled",
      outcome: "filled",
      before_at: "2026-08-02T10:30:00.000Z",
      before_id: "22222222-2222-4222-8222-222222222222",
    });
  });

  it("keeps active filters in CSV links and exposes complete decision JSON", () => {
    const decisionCsv = new URL(
      decisionCsvUrl("experiment-1", DECISION_FILTERS),
      "http://mission-control.local",
    );
    const tradeCsv = new URL(
      tradeCsvUrl("experiment-1", TRADE_FILTERS),
      "http://mission-control.local",
    );

    expect(decisionCsv.pathname).toBe(
      "/api/v1/experiments/experiment-1/decisions/export.csv",
    );
    expect(decisionCsv.searchParams.get("agent_name")).toBe("trend");
    expect(decisionCsv.searchParams.get("outcome")).toBe("filled");
    expect(decisionCsv.searchParams.has("limit")).toBe(false);
    expect(tradeCsv.pathname).toBe(
      "/api/v1/experiments/experiment-1/trades/export.csv",
    );
    expect(tradeCsv.searchParams.get("order_status")).toBe("filled");
    expect(decisionJsonUrl("decision-1")).toBe(
      "/api/v1/decisions/decision-1/export.json",
    );
  });
});
