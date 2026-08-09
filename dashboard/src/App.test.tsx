// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import * as auth from "./auth";
import App, {
  DecisionTable,
  ModelBoundary,
  OperatorConsole,
  ResearchLab,
  TradeTable,
} from "./App";
import type {
  DecisionDetail,
  DecisionPage,
  JsonRecord,
  ExperimentListItem,
  PaperModelAssumptions,
  OperatorCommandPage,
  ResearchLabView,
  RuntimeOverview,
  TradePage,
} from "./types";

const AUTHENTICATED: auth.AuthState = {
  status: "authenticated",
  actor: "sole_operator",
  authMode: "operator_session",
  expiresAt: "2026-08-09T18:00:00Z",
  csrfToken: "memory-only-csrf",
};

beforeEach(() => {
  vi.spyOn(auth, "restoreOperatorSession").mockResolvedValue(AUTHENTICATED);
});

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

  it("returns to login when the server expires the session", async () => {
    vi.spyOn(api, "listExperiments").mockRejectedValue(new api.SessionExpiredError());
    const logout = vi.spyOn(auth, "logoutOperator");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sign in to Mission Control" }))
      .toBeInTheDocument();
    expect(screen.getByText(/session expired/i)).toBeInTheDocument();
    expect(logout).not.toHaveBeenCalled();
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

const EMPTY_RESEARCH: ResearchLabView = {
  official_account_inclusion: "excluded",
  analytics_as_of: null,
  equity_curve: [],
  cost_waterfall: {
    initial_capital: "10000", gross_realized_pnl: "0", fees: "0", funding: "0",
    unrealized_pnl: "0", net_change: "0", ending_equity: "10000", reconciles: true,
  },
  performance: {
    basis: "fifo_closed_fill_allocations_net_of_open_and_close_fees_ex_funding",
    closed_trade_allocations: 0, wins: 0, losses: 0, breakeven: 0, win_rate: null,
    average_win: null, average_loss: null, expectancy: null, profit_factor: null,
    average_r_multiple: null, maximum_favorable_excursion: null,
    maximum_adverse_excursion: null,
  },
  attribution: {
    by_symbol: [], by_regime: [], by_strategy: [], by_agent_coalition: [],
    by_hour_berlin: [], by_direction: [], by_exit_reason: [],
  },
  calibration: {
    consensus: { sample_size: 0, brier_score: null, mean_probability: null, observed_win_rate: null },
  },
  gate_value: { interpretation: "", resolved_sample_size: 0, by_gate: [] },
  cost_sensitivity: {},
  benchmarks: {
    buy_and_hold: { status: "unavailable", ending_equity: null },
    flat_cash: { status: "available", ending_equity: "10000" },
  },
  availability: {
    funding_attribution: { status: "unavailable", reason: "not_allocated", sample_size: 0 },
  },
  counterfactuals: [],
  execution_sensitivities: [],
  limit_per_kind: 500,
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
    vi.spyOn(api, "getResearch").mockResolvedValue(EMPTY_RESEARCH);
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

  it("does not recompute full research analytics for every durable event", async () => {
    vi.spyOn(api, "listExperiments").mockResolvedValue([EVENT_FEED_EXPERIMENT]);
    const overview = vi.spyOn(api, "getOverview").mockResolvedValue({
      ...EVENT_FEED_EXPERIMENT,
      positions: [],
      pending_orders: [],
      incidents: [],
    });
    vi.spyOn(api, "listDecisions").mockResolvedValue({
      items: [], limit: 200, has_more: false, next_before_at: null, next_before_id: null,
    });
    vi.spyOn(api, "listTrades").mockResolvedValue({
      items: [], limit: 200, has_more: false, next_before_at: null, next_before_id: null,
    });
    vi.spyOn(api, "listCommands").mockResolvedValue({ items: [], limit: 100 });
    const research = vi.spyOn(api, "getResearch").mockResolvedValue(EMPTY_RESEARCH);
    let invalidate: (() => void) | undefined;
    vi.spyOn(api, "startResumableEventFeed").mockImplementation((options) => {
      invalidate = options.onEvents;
      return () => undefined;
    });

    render(<App />);
    await waitFor(() => expect(research).toHaveBeenCalledTimes(1));
    const initialOverviewCalls = overview.mock.calls.length;

    act(() => invalidate?.());

    await waitFor(() => expect(overview.mock.calls.length).toBeGreaterThan(initialOverviewCalls));
    expect(research).toHaveBeenCalledTimes(1);
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
      strategy_version_id: "99999999-9999-4999-8999-999999999999",
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
      outcome: "filled",
    },
  ],
  limit: 100,
  has_more: false,
  next_before_at: null,
  next_before_id: null,
};

const DECISION_PAGE: DecisionPage = {
  items: [
    {
      id: "22222222-2222-4222-8222-222222222222",
      experiment_id: "33333333-3333-4333-8333-333333333333",
      market_frame_id: "44444444-4444-4444-8444-444444444444",
      strategy_version_id: "99999999-9999-4999-8999-999999999999",
      symbol: "BTCUSDT",
      timeframe: "1m",
      cycle_at: "2026-08-02T12:00:00Z",
      regime: "trending",
      status: "completed",
      direction: "long",
      disposition: "approved",
      reason_code: "accepted",
      quality_status: "passed",
      consensus_direction: "long",
      consensus_probability: "0.65",
      consensus_confidence: "0.70",
      proposal_status: "approved",
      order_status: "filled",
      counterfactual_status: null,
      outcome: "filled",
      created_at: "2026-08-02T12:00:00Z",
      completed_at: "2026-08-02T12:00:01Z",
    },
  ],
  limit: 200,
  has_more: true,
  next_before_at: "2026-08-02T12:00:00Z",
  next_before_id: "22222222-2222-4222-8222-222222222222",
};

const DECISION = DECISION_PAGE.items[0]!;

const DECISION_DETAIL: DecisionDetail = {
  decision: DECISION,
  cycle: { feature_snapshot_json: { ema_fast: "60020" } },
  market_frame: { id: DECISION.market_frame_id },
  quality_evaluations: [],
  agents: [],
  summary: null,
  gates: [],
  proposal: null,
  orders: [],
  counterfactual: null,
  incident: null,
  timeline: [],
  lineage_hashes: {
    experiment_manifest: "a".repeat(64),
    market_frame: "b".repeat(64),
    decision_cycle: "c".repeat(64),
  },
};

describe("Audit Ledger history", () => {
  it("shows final outcomes and exposes working newer and older navigation", () => {
    const older = vi.fn();
    const newer = vi.fn();
    render(
      <DecisionTable
        page={DECISION_PAGE}
        onOpen={() => undefined}
        onOlder={older}
        onNewer={newer}
        canGoNewer
      />,
    );

    expect(screen.getByText(/^filled$/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Older decisions/ }));
    fireEvent.click(screen.getByRole("button", { name: /Newer decisions/ }));
    expect(older).toHaveBeenCalledOnce();
    expect(newer).toHaveBeenCalledOnce();
  });

  it("wires the complete filters, exports, and keyset cursor through Mission Control", async () => {
    vi.spyOn(api, "listExperiments").mockResolvedValue([EVENT_FEED_EXPERIMENT]);
    vi.spyOn(api, "getOverview").mockResolvedValue({
      ...EVENT_FEED_EXPERIMENT,
      positions: [],
      pending_orders: [],
      incidents: [],
    });
    const decisions = vi.spyOn(api, "listDecisions").mockResolvedValue(DECISION_PAGE);
    vi.spyOn(api, "getDecision").mockResolvedValue(DECISION_DETAIL);
    vi.spyOn(api, "listTrades").mockResolvedValue({
      items: [],
      limit: 200,
      has_more: false,
      next_before_at: null,
      next_before_id: null,
    });
    vi.spyOn(api, "listCommands").mockResolvedValue({ items: [], limit: 100 });
    vi.spyOn(api, "getResearch").mockResolvedValue(EMPTY_RESEARCH);
    vi.spyOn(api, "startResumableEventFeed").mockReturnValue(() => undefined);

    render(<App />);

    const filters = await screen.findByRole("group", { name: "Audit Ledger filters" });
    expect(within(filters).getByLabelText("Direction")).toBeInTheDocument();
    expect(within(filters).getByLabelText("Gate type")).toBeInTheDocument();
    expect(within(filters).getByLabelText("Agent name")).toBeInTheDocument();
    expect(within(filters).getByLabelText("Strategy version ID")).toBeInTheDocument();
    expect(within(filters).getByLabelText("Order status")).toBeInTheDocument();
    expect(within(filters).getByLabelText("Outcome")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Download filtered decisions CSV" }))
      .toHaveAttribute("href", expect.stringContaining("/decisions/export.csv"));

    fireEvent.click(await screen.findByRole("button", { name: /Older decisions/ }));
    await waitFor(() => {
      expect(decisions).toHaveBeenLastCalledWith(
        EVENT_FEED_EXPERIMENT.experiment.id,
        expect.any(Object),
        {
          beforeAt: DECISION_PAGE.next_before_at,
          beforeId: DECISION_PAGE.next_before_id,
        },
        expect.any(AbortSignal),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: /Audit/ }));
    expect(await screen.findByRole("link", { name: "Download complete JSON" }))
      .toHaveAttribute(
        "href",
        `/api/v1/decisions/${DECISION.id}/export.json`,
      );
  });
});

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
  it("requires the reason and exact phrase before queuing a visible command", () => {
    const submit = vi.fn();
    render(
      <OperatorConsole
        commands={COMMANDS}
        runtime={RUNTIME}
        incidents={[]}
        controlsEnabled
        busy={false}
        error={null}
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
        controlsEnabled
        busy={false}
        error={null}
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
  analytics_as_of: "2026-08-03T12:00:00Z",
  equity_curve: [
    { at: "2026-08-02T12:00:00Z", equity: "10000", drawdown: "0" },
    { at: "2026-08-03T12:00:00Z", equity: "10082", drawdown: "0.01" },
  ],
  cost_waterfall: {
    initial_capital: "10000",
    gross_realized_pnl: "100",
    fees: "-10",
    funding: "-3",
    unrealized_pnl: "-5",
    net_change: "82",
    ending_equity: "10082",
    reconciles: true,
  },
  performance: {
    basis: "fifo_closed_fill_allocations_net_of_open_and_close_fees_ex_funding",
    closed_trade_allocations: 4,
    wins: 3,
    losses: 1,
    breakeven: 0,
    win_rate: "0.75",
    average_win: "35",
    average_loss: "-23",
    expectancy: "20.5",
    profit_factor: "4.565",
    average_r_multiple: "1.4",
    maximum_favorable_excursion: "140",
    maximum_adverse_excursion: "45",
  },
  attribution: {
    by_symbol: [{ key: "BTCUSDT", trades: 4, wins: 3, losses: 1, win_rate: "0.75", net_pnl_ex_funding: "82", expectancy: "20.5" }],
    by_regime: [],
    by_strategy: [],
    by_agent_coalition: [],
    by_hour_berlin: [],
    by_direction: [],
    by_exit_reason: [],
  },
  calibration: {
    consensus: { sample_size: 4, brier_score: "0.19", mean_probability: "0.68", observed_win_rate: "0.75" },
    momentum: { sample_size: 4, brier_score: "0.17", mean_probability: "0.71", observed_win_rate: "0.75" },
  },
  gate_value: {
    interpretation: "positive_avoided_pnl_means_the_rejection_avoided_a_loss",
    resolved_sample_size: 1,
    by_gate: [{ gate: "monitoring", sample_size: 1, hypothetical_pnl: "-82", avoided_pnl: "82" }],
  },
  cost_sensitivity: {
    optimistic: { sample_size: 1, execution_cost: "4", marked_pnl: "90" },
    stress: { sample_size: 1, execution_cost: "9", marked_pnl: "76" },
  },
  benchmarks: {
    buy_and_hold: { status: "available", method: "equal_weight_long_first_to_last_observed_close_no_costs", symbols: 1, return: "0.04", ending_equity: "10400", returns_by_symbol: { BTCUSDT: "0.04" } },
    flat_cash: { status: "available", method: "initial_capital_held_in_cash_zero_interest", return: "0", ending_equity: "10000" },
  },
  availability: {
    closed_trade_metrics: { status: "available", reason: null, sample_size: 4 },
    mfe_mae: { status: "available", reason: null, sample_size: 4 },
    r_multiples: { status: "available", reason: null, sample_size: 4 },
    calibration: { status: "available", reason: null, sample_size: 4 },
    gate_value: { status: "available", reason: null, sample_size: 1 },
    funding_attribution: { status: "unavailable", reason: "funding_is_authoritative_at_account_level_but_not_allocated_to_close_fills", sample_size: 0 },
  },
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
    expect(screen.getAllByText(/82\.00 USDT/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/75\.00%/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Brier score/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Buy and hold/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/Funding attribution unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/rejected at monitoring/i)).toBeInTheDocument();
    expect(screen.getByText(/^stress$/i)).toBeInTheDocument();
    expect(screen.getByText(/-24\.00 USDT/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect BTCUSDT counterfactual" }));
    expect(open).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
  });
});
