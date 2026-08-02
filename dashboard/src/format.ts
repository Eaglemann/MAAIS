const money = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 2,
});

export function formatMoney(value: string | number, currency = "USDT"): string {
  const parsed = Number(value);
  return `${money.format(Number.isFinite(parsed) ? parsed : 0)} ${currency}`;
}

export function formatCompact(value: string | number): string {
  const parsed = Number(value);
  return compact.format(Number.isFinite(parsed) ? parsed : 0);
}

export function formatPercent(value: string | number): string {
  const parsed = Number(value);
  return `${money.format((Number.isFinite(parsed) ? parsed : 0) * 100)}%`;
}

export function formatTime(value: string | null): string {
  if (!value) return "Not recorded";
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return "Invalid timestamp";
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  }).format(timestamp);
}

export function shortHash(value: string | null): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : "—";
}

export function label(value: unknown): string {
  return String(value ?? "—").replaceAll("_", " ");
}

export type Tone = "good" | "warn" | "bad" | "muted" | "info";

export function statusTone(value: string | null): Tone {
  if (!value) return "muted";
  if (["running", "active", "passed", "approved", "filled", "completed"].includes(value)) {
    return "good";
  }
  if (["failed", "halted", "critical", "error", "rejected"].includes(value)) {
    return "bad";
  }
  if (["quarantined", "warning", "recovering", "pending", "open"].includes(value)) {
    return "warn";
  }
  return "info";
}
