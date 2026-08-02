import type {
  DecisionDetail,
  DecisionFilters,
  DecisionPage,
  ExperimentListItem,
  ExperimentOverview,
  TradePage,
} from "./types";

const API_ROOT = import.meta.env.VITE_API_ROOT ?? "/api/v1";

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The HTTP status remains the authoritative fallback.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function listExperiments(signal?: AbortSignal): Promise<ExperimentListItem[]> {
  return getJson<ExperimentListItem[]>("/experiments", signal);
}

export function getOverview(
  experimentId: string,
  signal?: AbortSignal,
): Promise<ExperimentOverview> {
  return getJson<ExperimentOverview>(`/experiments/${experimentId}/overview`, signal);
}

export function listDecisions(
  experimentId: string,
  filters: DecisionFilters,
  signal?: AbortSignal,
): Promise<DecisionPage> {
  const params = new URLSearchParams({ limit: "200" });
  if (filters.symbol.trim()) params.set("symbol", filters.symbol.trim().toUpperCase());
  if (filters.status) params.set("status", filters.status);
  if (filters.disposition) params.set("disposition", filters.disposition);
  if (filters.reasonCode.trim()) params.set("reason_code", filters.reasonCode.trim());
  return getJson<DecisionPage>(
    `/experiments/${experimentId}/decisions?${params.toString()}`,
    signal,
  );
}

export function listTrades(
  experimentId: string,
  symbol: string,
  signal?: AbortSignal,
): Promise<TradePage> {
  const params = new URLSearchParams({ limit: "200" });
  if (symbol.trim()) params.set("symbol", symbol.trim().toUpperCase());
  return getJson<TradePage>(
    `/experiments/${experimentId}/trades?${params.toString()}`,
    signal,
  );
}

export function getDecision(
  decisionId: string,
  signal?: AbortSignal,
): Promise<DecisionDetail> {
  return getJson<DecisionDetail>(`/decisions/${decisionId}`, signal);
}
