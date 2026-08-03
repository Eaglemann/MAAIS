// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import App, { ModelBoundary, OperatorConsole, ResearchLab, TradeTable } from "./App";
import type {
  JsonRecord,
  ExperimentListItem,
  PaperModelAssumptions,
  OperatorCommandPage,
  ResearchLabView,
  RuntimeOverview,
  TradePage,
} from "./types";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe("Mission Control startup", () => {
  it("shows the clean-database empty state after the first experiment query", async () => {
    vi.spyOn(api, "listExperiments").mockResolvedValue([]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "No paper experiments yet" }))
      .toBeInTheDocument();
  });
});

const MODEL_ASSUMPTIONS: PaperModelAssumptions = {
  model_status: "frozen_paper_model",
  leverage: 1,
  maintenance_margin_model: "fixed_fraction_of_gross_notional",
  maintenance_margin_rate: "0.005",
  liquidation_price_model: "not_modeled",
  exchange_liquidation_parity: false,
  limitations: ["exchange_liquidation_behavior_not_modeled"],
};

const EVENT_FEED_EXPERIMENT: ExperimentListItem = {
  experiment: {
    id: "22222222-2222-4222-8222-222222222222",
    name: "paper-week",
    mode: "paper_live",
    status: "running",
    initial_capital: "10000",
    currency: "USDT",
    created_at: "2026-08-02T12:00:00Z",
    started_at: "2026-08-02T12:00:01Z",
    ended_at: null,
    failure_reason: null,
    git_sha: "a".repeat(40),
    worktree_hash: null,
    lock_hash: "b".repeat(64),
    schema_revision: "0017",
    config_hash: "c".repeat(64),
    manifest_hash: "d".repeat(64),
    manifest_schema_version: 2,
    model_assumptions: MODEL_ASSUMPTIONS,
  },
  account: {
    source: "manifest_initial_state",
    snapshot_at: null,
    account_version: 0,
    cash_balance: "10000",
    equity: "10000",
    used_margin: "0",
    free_margin: "10000",
    gross_notional: "0",
    risk_at_stop: "0",
    unrealized_pnl: "0",
    realized_pnl: "0",
    fees: "0",
    funding: "0",
    peak_equity: "10000",
    drawdown: "0",
  },
  runtime: {
    worker_status: "running",
    checkpoint_at: "2026-08-02T12:00:01Z",
    checkpoint_version: 1,
    lease_status: "active",
    lease_heartbeat_at: "2026-08-02T12:00:01Z",
    lease_expires_at: "2026-08-02T12:01:01Z",
    lease_released_at: null,
    lease_epoch: 1,
    kill_switch_active: false,
    kill_switch_reason: null,
    control_version: 1,
  },
  decisions: {
    total: 0,
    completed: 0,
    rejected: 0,
    quarantined: 0,
    neutral: 0,
    approved: 0,
    directional_rejected: 0,
  },
  operations: {
    open_positions: 0,
    pending_orders: 0,
    fills: 0,
    open_incidents: 0,
    review_incidents: 0,
    pending_counterfactuals: 0,
  },
  freshness: {
    expected_symbols: 10,
    cursor_count: 10,
    latest_bar_close_at: "2026-08-02T12:00:00Z",
    latest_cursor_update_at: "2026-08-02T12:00:01Z",
    halted_cursors: 0,
    active_recoveries: 0,
  },
};

describe("Mission Control event synchronization", () => {
  it("starts a fresh browser tab at the current durable event head", async () => {
    vi.spyOn(api, "listExperiments").mockResolvedValue([EVENT_FEED_EXPERIMENT]);
    vi.spyOn(api, "getOverview").mockResolvedValue({
      ...EVENT_FEED_EXPERIMENT,
      positions: [],
      pending_orders: [],
      incidents: [],
    });
    vi.spyOn(api, "listDecisions").mockResolvedValue({
      items: [],
      limit: 200,
      has_more: false,
      next_before_at: null,
      next_before_id: null,
    });
    vi.spyOn(api, "listTrades").mockResolvedValue({
      items: [],
      limit: 200,
      has_more: false,
      next_before_at: null,
      next_before_id: null,
    });
    vi.spyOn(api, "listCommands").mockResolvedValue({ items: [], limit: 100 });
    vi.spyOn(api, "getResearch").mockResolvedValue({
      official_account_inclusion: "excluded",
      counterfactuals: [],
      execution_sensitivities: [],
      limit_per_kind: 500,
    });
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        items: [],
        limit: 500,
        has_more: false,
        next_cursor: 42,
      }),
    }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", class {
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onclose: (() => void) | null = null;
      onerror: (() => void) | null = null;
      close() {}
    });

    render(<App />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(`/events?after_cursor=${Number.MAX_SAFE_INTEGER}`),
      expect.any(Object),
    );
  });
});

describe("Paper model boundary", () => {
  it("makes the maintenance-margin approximation and absent liquidation parity explicit", () => {
    render(<ModelBoundary assumptions={MODEL_ASSUMPTIONS} />);

    expect(screen.getByRole("heading", { name: "Simulation model boundary" }))
      .toBeInTheDocument();
    expect(screen.getByText(/0\.5% of gross notional/i)).toBeInTheDocument();
    expect(screen.getByText(/liquidation price is not modeled/i)).toBeInTheDocument();
    expect(screen.getByText(/does not reproduce exchange liquidation behavior/i))
      .toBeInTheDocument();
    expect(screen.getByText(/1x leverage/i)).toBeInTheDocument();
    expect(screen.getByText(/no exchange liquidation parity/i)).toBeInTheDocument();
  });
});

const PAGE: TradePage = {
  items: [
    {
      proposal_id: "11111111-1111-4111-8111-111111111111",
      decision_cycle_id: "22222222-2222-4222-8222-222222222222",
      proposed_at: "2026-08-02T12:00:00Z",
      latest_activity_at: "2026-08-02T12:00:01Z",
      symbol: "BTCUSDT",
      direction: "long",
      proposal_status: "approved",
      proposal_reason_code: "accepted",
      approved_notional: "6000",
      decision_disposition: "approved",
      decision_reason_code: "accepted",
      regime: "trending",
      official_order_count: 1,
      order_statuses: ["filled"],
      fill_count: 1,
      filled_quantity: "0.1",
      gross_fill_notional: "6000",
      fees: "3",
      total_slippage: "0.07",
      counterfactual_status: null,
      counterfactual_pnl: null,
    },
  ],
  limit: 100,
  has_more: false,
  next_before_at: null,
  next_before_id: null,
};

function Harness() {
  const [selected, setSelected] = useState("none");
  return (
    <>
      <TradeTable page={PAGE} currency="USDT" onOpen={setSelected} />
      <output aria-label="Selected decision">{selected}</output>
    </>
  );
}

describe("Trade Ledger", () => {
  it("shows official fill costs and opens the exact linked decision", () => {
    render(<Harness />);

    expect(screen.getByText("BTCUSDT")).toBeInTheDocument();
    expect(screen.getByText("1 fill")).toBeInTheDocument();
    expect(screen.getByText(/fees 3\.00 USDT/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect BTCUSDT proposal" }));
    expect(screen.getByLabelText("Selected decision")).toHaveTextContent(
      "22222222-2222-4222-8222-222222222222",
    );
  });
});

const RUNTIME: RuntimeOverview = {
  worker_status: "running",
  checkpoint_at: "2026-08-02T12:00:00Z",
  checkpoint_version: 3,
  lease_status: "active",
  lease_heartbeat_at: "2026-08-02T12:00:00Z",
  lease_expires_at: "2026-08-02T12:01:00Z",
  lease_released_at: null,
  lease_epoch: 2,
  kill_switch_active: false,
  kill_switch_reason: null,
  control_version: 7,
};

const COMMANDS: OperatorCommandPage = {
  limit: 100,
  items: [
    {
      command_id: "33333333-3333-4333-8333-333333333333",
      experiment_id: "22222222-2222-4222-8222-222222222222",
      command_type: "emergency_halt",
      status: "completed",
      idempotency_key: "halt-0001",
      actor: "local_operator",
      reason: "operator observed abnormal behavior",
      payload: { source: "mission_control" },
      operator_confirmed: true,
      request_hash: "a".repeat(64),
      requested_at: "2026-08-02T12:00:00Z",
      version: 3,
      accepted_at: "2026-08-02T12:00:01Z",
      accepted_by: "paper_worker:test",
      completed_at: "2026-08-02T12:00:02Z",
      result: { experiment_status: "paused", kill_switch_active: true },
    },
  ],
};

describe("Operator Console", () => {
  it("requires the local token, reason, and exact phrase before queuing a visible command", () => {
    const submit = vi.fn();
    render(
      <OperatorConsole
        commands={COMMANDS}
        runtime={RUNTIME}
        incidents={[]}
        token="local-session-token"
        busy={false}
        error={null}
        onTokenChange={() => undefined}
        onSubmit={submit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Pause worker" }));
    fireEvent.change(screen.getByLabelText("Operator reason"), {
      target: { value: "review unexpected signal concentration" },
    });
    fireEvent.change(screen.getByLabelText("Exact confirmation phrase"), {
      target: { value: "CONFIRM PAUSE" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Queue confirmed command" }));

    expect(submit).toHaveBeenCalledWith(
      {
        commandType: "pause",
        reason: "review unexpected signal concentration",
        payload: {},
        confirmation: "CONFIRM PAUSE",
      },
      "local-session-token",
    );
    expect(screen.getByText("operator observed abnormal behavior")).toBeInTheDocument();
    expect(screen.getByText(/paper_worker:test/)).toBeInTheDocument();
    expect(screen.getByText(/kill_switch_active/)).toBeInTheDocument();
  });

  it("prepares incident actions and current control lineage explicitly", () => {
    const submit = vi.fn();
    const incidents: JsonRecord[] = [
      {
        id: "44444444-4444-4444-8444-444444444444",
        status: "open",
        severity: "critical",
        reason_code: "stale_market_data",
      },
    ];
    render(
      <OperatorConsole
        commands={COMMANDS}
        runtime={{ ...RUNTIME, kill_switch_active: true, kill_switch_reason: "system_halt:test" }}
        incidents={incidents}
        token="local-session-token"
        busy={false}
        error={null}
        onTokenChange={() => undefined}
        onSubmit={submit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reset kill switch" }));
    expect(screen.getByText(/expected control version 7/i)).toBeInTheDocument();
    expect(screen.getByText(/system_halt:test/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Acknowledge incident" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resolve incident" })).toBeInTheDocument();
  });
});

const RESEARCH: ResearchLabView = {
  official_account_inclusion: "excluded",
  limit_per_kind: 500,
  counterfactuals: [
    {
      id: "55555555-5555-4555-8555-555555555555",
      proposal_id: "11111111-1111-4111-8111-111111111111",
      decision_cycle_id: "22222222-2222-4222-8222-222222222222",
      symbol: "BTCUSDT",
      direction: "long",
      rejection_gate: "monitoring",
      status: "resolved",
      maximum_favorable_excursion: "120",
      maximum_adverse_excursion: "35",
      outcome_15m: "18",
      outcome_1h: "44",
      outcome_4h: "71",
      outcome_24h: "95",
      no_fill_reason: null,
      hypothetical_exit_reason: "time_exit",
      hypothetical_pnl: "82",
      created_at: "2026-08-02T12:00:00Z",
      closed_at: "2026-08-03T12:00:00Z",
      content_hash: "b".repeat(64),
    },
  ],
  execution_sensitivities: [
    {
      id: "66666666-6666-4666-8666-666666666666",
      order_intent_id: "77777777-7777-4777-8777-777777777777",
      proposal_id: "11111111-1111-4111-8111-111111111111",
      decision_cycle_id: "22222222-2222-4222-8222-222222222222",
      symbol: "BTCUSDT",
      scenario: "stress",
      calculated_at: "2026-08-02T12:00:01Z",
      outcome: { marked_pnl: "-24", execution_cost: "9" },
    },
  ],
};

describe("Research Lab", () => {
  it("keeps hypothetical outcomes separate and opens their official decision lineage", () => {
    const open = vi.fn();
    render(<ResearchLab research={RESEARCH} currency="USDT" onOpen={open} />);

    expect(screen.getByText(/excluded from official account P&L/i)).toBeInTheDocument();
    expect(screen.getByText(/rejected at monitoring/i)).toBeInTheDocument();
    expect(screen.getByText(/^stress$/i)).toBeInTheDocument();
    expect(screen.getByText(/-24\.00 USDT/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect BTCUSDT counterfactual" }));
    expect(open).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
  });
});
