// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperatorConsole } from "./OperatorConsole";

afterEach(cleanup);

describe("secure Operator Console", () => {
  it("queues a confirmed command without asking for a browser token", () => {
    const submit = vi.fn();
    render(
      <OperatorConsole
        commands={null}
        runtime={null}
        incidents={[]}
        controlsEnabled
        busy={false}
        error={null}
        onSubmit={submit}
      />,
    );

    expect(screen.queryByLabelText(/control token/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause worker" }));
    fireEvent.change(screen.getByLabelText("Operator reason"), {
      target: { value: "review unexpected signal concentration" },
    });
    fireEvent.change(screen.getByLabelText("Exact confirmation phrase"), {
      target: { value: "CONFIRM PAUSE" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Queue confirmed command" }));

    expect(submit).toHaveBeenCalledWith({
      commandType: "pause",
      reason: "review unexpected signal concentration",
      payload: {},
      confirmation: "CONFIRM PAUSE",
    });
  });

  it("keeps commands disabled in local read-only compatibility mode", () => {
    render(
      <OperatorConsole
        commands={null}
        runtime={null}
        incidents={[]}
        controlsEnabled={false}
        busy={false}
        error={null}
        onSubmit={() => undefined}
      />,
    );

    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause worker" })).toBeDisabled();
  });
});
