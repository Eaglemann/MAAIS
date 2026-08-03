// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperatorConsole, ResearchLab, TradeTable } from "./App";
import type {
  JsonRecord,
  OperatorCommandPage,
  ResearchLabView,
  RuntimeOverview,
  TradePage,
} from "./types";

afterEach(cleanup);

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
