// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { TradeTable } from "./App";
import type { TradePage } from "./types";

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
