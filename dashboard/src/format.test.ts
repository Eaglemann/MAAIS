import { describe, expect, it } from "vitest";

import { formatMoney, formatPercent, formatTime, shortHash, statusTone } from "./format";

describe("Mission Control formatting", () => {
  it("renders authoritative decimal strings without floating UI noise", () => {
    expect(formatMoney("10000.000000000000000000")).toBe("10,000.00 USDT");
    expect(formatPercent("0.025")).toBe("2.50%");
  });

  it("uses safety-oriented tones", () => {
    expect(statusTone("running")).toBe("good");
    expect(statusTone("quarantined")).toBe("warn");
    expect(statusTone("halted")).toBe("bad");
  });

  it("keeps both ends of immutable hashes visible", () => {
    expect(shortHash("1234567890abcdef")).toBe("12345678…abcdef");
  });

  it("does not crash the workstation on a malformed upstream timestamp", () => {
    expect(formatTime("—")).toBe("Invalid timestamp");
  });
});
