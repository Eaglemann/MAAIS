import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getDecision,
  getOverview,
  getResearch,
  listCommands,
  listDecisions,
  listExperiments,
  listTrades,
  requestOperatorCommand,
  startResumableEventFeed,
} from "./api";
import {
  formatCompact,
  formatMoney,
  formatPercent,
  formatTime,
  label,
  shortHash,
  statusTone,
  type Tone,
} from "./format";
import type {
  DecisionDetail,
  DecisionFilters,
  DecisionListItem,
  DecisionPage,
  ExperimentListItem,
  ExperimentOverview,
  EventFeedStatus,
  JsonRecord,
  OperatorActionDraft,
  OperatorCommandPage,
  PaperModelAssumptions,
  ResearchLabView,
  TradePage,
} from "./types";
import { OperatorConsole } from "./OperatorConsole";
import { ResearchLab } from "./ResearchLab";

export { OperatorConsole, ResearchLab };

const EMPTY_FILTERS: DecisionFilters = {
  symbol: "",
  status: "",
  disposition: "",
  reasonCode: "",
};

function recordValue(record: JsonRecord, key: string): unknown {
  return record[key];
}

function recordString(record: JsonRecord, key: string): string {
  return String(recordValue(record, key) ?? "—");
}

function Badge({ value, tone }: { value: string | null; tone?: Tone }) {
  return <span className={`badge badge--${tone ?? statusTone(value)}`}>{label(value)}</span>;
}

function MetricCard({
  eyebrow,
  value,
  note,
  tone = "muted",
}: {
  eyebrow: string;
  value: string;
  note: string;
  tone?: Tone;
}) {
  return (
    <article className={`metric-card metric-card--${tone}`}>
      <span className="metric-card__eyebrow">{eyebrow}</span>
      <strong>{value}</strong>
      <span className="metric-card__note">{note}</span>
    </article>
  );
}

function SectionHeader({
  title,
  subtitle,
  aside,
}: {
  title: string;
  subtitle: string;
  aside?: React.ReactNode;
}) {
  return (
    <div className="section-header">
      <div>
        <h2>{title}</h2>
        <p>{subtitle}</p>
      </div>
      {aside}
    </div>
  );
}

function JsonDetails({ labelText, value }: { labelText: string; value: unknown }) {
  return (
    <details className="json-details">
      <summary>{labelText}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function ModelBoundary({
  assumptions,
}: {
  assumptions: PaperModelAssumptions | null;
}) {
  const rate = Number(assumptions?.maintenance_margin_rate ?? Number.NaN);
  const rateLabel = Number.isFinite(rate)
    ? `${rate * 100}% of gross notional`
    : "not disclosed";
  const leverage = assumptions?.leverage === null || assumptions?.leverage === undefined
    ? "unknown leverage"
    : `${assumptions.leverage}x leverage`;
  const parity = assumptions?.exchange_liquidation_parity === false
    ? "no exchange liquidation parity"
    : "exchange liquidation parity is not established";
  const liquidation = assumptions?.liquidation_price_model === "not_modeled"
    ? "Liquidation price is not modeled."
    : "Liquidation model is not disclosed or supported for this experiment.";
  return (
    <section className="model-boundary" aria-label="Paper model limitations">
      <div>
        <span className="kicker">Known simulation limitation</span>
        <h2>Simulation model boundary</h2>
      </div>
      <div className="model-boundary__facts">
        <span>Maintenance margin is {rateLabel}.</span>
        <span>{liquidation}</span>
        <span>{leverage} · {parity}.</span>
      </div>
      <strong>This paper model does not reproduce exchange liquidation behavior.</strong>
    </section>
  );
}

function DecisionDrawer({
  detail,
  loading,
  error,
  onClose,
}: {
  detail: DecisionDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  const decision = detail?.decision;
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside
        className="audit-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Decision audit bundle"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="audit-drawer__header">
          <div>
            <span className="kicker">Decision audit bundle</span>
            <h2>{decision ? `${decision.symbol} · ${formatTime(decision.cycle_at)}` : "Loading"}</h2>
            {decision && (
              <div className="badge-row">
                <Badge value={decision.status} />
                <Badge value={decision.disposition} />
                <Badge value={decision.quality_status} />
              </div>
            )}
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close audit">
            ×
          </button>
        </header>

        {loading && <div className="drawer-state">Loading the authoritative audit bundle…</div>}
        {error && <div className="error-panel">{error}</div>}
        {detail && decision && (
          <div className="audit-drawer__body">
            <section className="audit-section audit-section--why">
              <span className="kicker">Why this happened</span>
              <h3>{label(decision.reason_code)}</h3>
              <p>
                The cycle finished as <strong>{label(decision.disposition)}</strong> in the{" "}
                <strong>{label(decision.regime)}</strong> regime. Every source record below is tied
                to the same immutable frame and decision hashes.
              </p>
              <div className="hash-grid">
                {Object.entries(detail.lineage_hashes).map(([name, hash]) => (
                  <div key={name}>
                    <span>{label(name)}</span>
                    <code title={hash}>{shortHash(hash)}</code>
                  </div>
                ))}
              </div>
            </section>

            <section className="audit-section">
              <SectionHeader
                title="Data integrity"
                subtitle={`${detail.quality_evaluations.length} checks at the exact decision cutoff`}
              />
              <div className="check-grid">
                {detail.quality_evaluations.map((check) => {
                  const id = recordString(check, "id");
                  const status = recordString(check, "status");
                  return (
                    <article className="check-card" key={id}>
                      <div>
                        <strong>{label(recordString(check, "check_name"))}</strong>
                        <Badge value={status} />
                      </div>
                      <span>{label(recordString(check, "reason_code"))}</span>
                      <JsonDetails labelText="Evidence" value={recordValue(check, "details_json")} />
                    </article>
                  );
                })}
              </div>
              <JsonDetails labelText="Causal market frame and source manifest" value={detail.market_frame} />
              <JsonDetails labelText="Feature snapshot" value={recordValue(detail.cycle, "feature_snapshot_json")} />
            </section>

            <section className="audit-section">
              <SectionHeader
                title="Agent panel"
                subtitle="All eight agent outputs, weights, confidence, and explanation metadata"
              />
              <div className="agent-grid">
                {detail.agents.map((agent) => (
                  <article className="agent-card" key={recordString(agent, "id")}>
                    <div className="agent-card__header">
                      <div>
                        <strong>{label(recordString(agent, "agent_name"))}</strong>
                        <span>{recordString(agent, "agent_version")} · {label(recordString(agent, "maturity"))}</span>
                      </div>
                      <Badge value={recordString(agent, "direction")} />
                    </div>
                    <dl className="mini-metrics">
                      <div><dt>Probability</dt><dd>{formatPercent(recordString(agent, "probability"))}</dd></div>
                      <div><dt>Confidence</dt><dd>{formatPercent(recordString(agent, "confidence"))}</dd></div>
                      <div><dt>Risk</dt><dd>{formatPercent(recordString(agent, "risk"))}</dd></div>
                      <div><dt>Weight</dt><dd>{recordString(agent, "weight")}</dd></div>
                    </dl>
                    <div className="reason-list">
                      {Array.isArray(recordValue(agent, "reason_codes_json")) &&
                        (recordValue(agent, "reason_codes_json") as unknown[]).map((reason) => (
                          <span key={String(reason)}>{label(reason)}</span>
                        ))}
                    </div>
                    <JsonDetails labelText="Inputs and explanation" value={{
                      input: recordValue(agent, "input_snapshot_json"),
                      explanation: recordValue(agent, "explanation_json"),
                      dependencies: recordValue(agent, "data_dependencies"),
                    }} />
                  </article>
                ))}
              </div>
            </section>

            <section className="audit-section">
              <SectionHeader
                title="Decision gates"
                subtitle="Sequential gates stop at the first blocking result"
              />
              <div className="gate-list">
                {detail.gates.map((gate) => (
                  <article key={recordString(gate, "id")}>
                    <span className="gate-sequence">{recordString(gate, "sequence")}</span>
                    <div>
                      <strong>{label(recordString(gate, "gate_type"))}</strong>
                      <span>{label(recordString(gate, "reason_code"))}</span>
                    </div>
                    <Badge
                      value={recordValue(gate, "passed") === true ? "passed" : "failed"}
                    />
                    <JsonDetails labelText="Gate inputs and outputs" value={{
                      input: recordValue(gate, "input_json"),
                      output: recordValue(gate, "output_json"),
                    }} />
                  </article>
                ))}
              </div>
              <JsonDetails labelText="Consensus, adversarial challenge, EV, and costs" value={detail.summary} />
            </section>

            <section className="audit-section">
              <SectionHeader
                title="Proposal and execution"
                subtitle="Official and counterfactual paths remain visibly separate"
              />
              {!detail.proposal && <div className="empty-inline">No directional proposal was created.</div>}
              {detail.proposal && <JsonDetails labelText="Trade proposal" value={detail.proposal} />}
              {detail.orders.length > 0 ? (
                detail.orders.map((order) => (
                  <JsonDetails key={recordString(order, "id")} labelText={`Order ${recordString(order, "client_order_id")}`} value={order} />
                ))
              ) : (
                <div className="empty-inline">No official paper orders or fills exist for this cycle.</div>
              )}
              {detail.counterfactual && (
                <div className="counterfactual-panel">
                  <span className="kicker">Research only · excluded from account P&amp;L</span>
                  <JsonDetails labelText="Counterfactual outcome" value={detail.counterfactual} />
                </div>
              )}
            </section>

            {detail.incident && (
              <section className="audit-section">
                <SectionHeader title="Linked incident" subtitle="Operational review record for this frame" />
                <JsonDetails labelText={label(recordString(detail.incident, "reason_code"))} value={detail.incident} />
              </section>
            )}

            <section className="audit-section">
              <SectionHeader
                title="Immutable event timeline"
                subtitle={`${detail.timeline.length} ordered domain events`}
              />
              <ol className="timeline">
                {detail.timeline.map((event) => (
                  <li key={event.id}>
                    <span className="timeline__dot" />
                    <div>
                      <div className="timeline__title">
                        <strong>{label(event.event_type)}</strong>
                        <code>#{event.global_position}</code>
                      </div>
                      <span>{formatTime(event.occurred_at)} · {label(event.aggregate_type)} v{event.stream_version}</span>
                      <JsonDetails labelText="Event payload" value={{ payload: event.payload, metadata: event.metadata }} />
                    </div>
                  </li>
                ))}
              </ol>
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}

function DecisionTable({
  page,
  onOpen,
}: {
  page: DecisionPage | null;
  onOpen: (decision: DecisionListItem) => void;
}) {
  if (!page?.items.length) {
    return <div className="empty-state">No decisions match the current filters.</div>;
  }
  return (
    <div className="table-shell">
      <table className="decision-table">
        <thead>
          <tr>
            <th>Decision time</th>
            <th>Symbol</th>
            <th>Regime</th>
            <th>Outcome</th>
            <th>Why</th>
            <th>Consensus</th>
            <th>Quality</th>
            <th><span className="sr-only">Open</span></th>
          </tr>
        </thead>
        <tbody>
          {page.items.map((decision) => (
            <tr key={decision.id} onClick={() => onOpen(decision)}>
              <td><time>{formatTime(decision.cycle_at)}</time></td>
              <td><strong>{decision.symbol}</strong><span>{decision.timeframe}</span></td>
              <td>{label(decision.regime)}</td>
              <td><Badge value={decision.disposition} /></td>
              <td>{label(decision.reason_code)}</td>
              <td>
                <strong>{label(decision.consensus_direction)}</strong>
                <span>{decision.consensus_probability ? formatPercent(decision.consensus_probability) : "—"}</span>
              </td>
              <td><Badge value={decision.quality_status} /></td>
              <td><button type="button" className="row-action">Audit →</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {page.has_more && (
        <div className="table-footnote">Showing the latest {page.limit} cycles. Older cycles remain available through the API cursor.</div>
      )}
    </div>
  );
}

export function TradeTable({
  page,
  currency,
  onOpen,
}: {
  page: TradePage | null;
  currency: string;
  onOpen: (decisionId: string) => void;
}) {
  if (!page || page.items.length === 0) {
    return <div className="empty-state">No directional trade proposals exist yet.</div>;
  }
  return (
    <div className="table-shell">
      <table className="decision-table trade-table">
        <thead>
          <tr>
            <th>Proposed</th>
            <th>Market</th>
            <th>Decision</th>
            <th>Official execution</th>
            <th>Fill economics</th>
            <th>Research outcome</th>
          </tr>
        </thead>
        <tbody>
          {page.items.map((trade) => (
            <tr key={trade.proposal_id}>
              <td><time>{formatTime(trade.proposed_at)}</time><span>latest {formatTime(trade.latest_activity_at)}</span></td>
              <td>
                <button className="row-link" type="button" aria-label={`Inspect ${trade.symbol} proposal`} onClick={() => onOpen(trade.decision_cycle_id)}>{trade.symbol}</button>
                <span>{label(trade.direction)} · {label(trade.regime)}</span>
              </td>
              <td><Badge value={trade.decision_disposition} /><span>{label(trade.decision_reason_code)}</span></td>
              <td>
                <Badge value={trade.proposal_status} />
                <span>{trade.official_order_count} {trade.official_order_count === 1 ? "order" : "orders"} · {trade.order_statuses.map(label).join(", ") || "no order"}</span>
              </td>
              <td>
                <strong>{trade.fill_count} {trade.fill_count === 1 ? "fill" : "fills"}</strong>
                <span>{trade.filled_quantity} units · fees {formatMoney(trade.fees, currency)}</span>
                <span>modeled slippage {formatMoney(trade.total_slippage, currency)}</span>
              </td>
              <td>
                <Badge value={trade.counterfactual_status ?? "not_applicable"} />
                <span>{trade.counterfactual_pnl === null ? "Official path" : `Hypothetical ${formatMoney(trade.counterfactual_pnl, currency)}`}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {page.has_more && <div className="table-footnote">Showing the latest {page.limit} proposals. Older proposals remain available through the API cursor.</div>}
    </div>
  );
}

export default function App() {
  const [experiments, setExperiments] = useState<ExperimentListItem[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [overview, setOverview] = useState<ExperimentOverview | null>(null);
  const [decisionPage, setDecisionPage] = useState<DecisionPage | null>(null);
  const [tradePage, setTradePage] = useState<TradePage | null>(null);
  const [commands, setCommands] = useState<OperatorCommandPage | null>(null);
  const [research, setResearch] = useState<ResearchLabView | null>(null);
  const [filters, setFilters] = useState<DecisionFilters>(EMPTY_FILTERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [selectedDecision, setSelectedDecision] = useState<DecisionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [controlToken, setControlToken] = useState(() => {
    try {
      return window.sessionStorage.getItem("maais.control_token.v1") ?? "";
    } catch {
      return "";
    }
  });
  const [commandBusy, setCommandBusy] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [liveStatus, setLiveStatus] = useState<EventFeedStatus>("catching_up");

  useEffect(() => {
    try {
      if (controlToken) window.sessionStorage.setItem("maais.control_token.v1", controlToken);
      else window.sessionStorage.removeItem("maais.control_token.v1");
    } catch {
      // A blocked browser storage policy only removes tab-level convenience.
    }
  }, [controlToken]);

  useEffect(() => {
    const controller = new AbortController();
    listExperiments(controller.signal)
      .then((items) => {
        setExperiments(items);
        setSelectedId((current) => current || items[0]?.experiment.id || "");
        if (items.length === 0) setLoading(false);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : String(reason));
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!selectedId) return;
    try {
      const [
        nextOverview,
        nextDecisions,
        nextTrades,
        nextExperiments,
        nextCommands,
        nextResearch,
      ] = await Promise.all([
        getOverview(selectedId, signal),
        listDecisions(selectedId, filters, signal),
        listTrades(selectedId, filters.symbol, signal),
        listExperiments(signal),
        listCommands(selectedId, signal),
        getResearch(selectedId, signal),
      ]);
      setOverview(nextOverview);
      setDecisionPage(nextDecisions);
      setTradePage(nextTrades);
      setExperiments(nextExperiments);
      setCommands(nextCommands);
      setResearch(nextResearch);
      setLastUpdated(new Date().toISOString());
      setError(null);
    } catch (reason: unknown) {
      if (!signal?.aborted) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [filters, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setLoading(true);
    void refresh(controller.signal);
    const interval = window.setInterval(() => void refresh(controller.signal), 30_000);
    return () => {
      window.clearInterval(interval);
      controller.abort();
    };
  }, [refresh, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    let initialCursor = Number.MAX_SAFE_INTEGER;
    try {
      const storedValue = window.sessionStorage.getItem("maais.event_cursor.v2");
      const stored = storedValue === null ? null : Number(storedValue);
      if (stored !== null && Number.isSafeInteger(stored) && stored >= 0) {
        initialCursor = stored;
      }
    } catch {
      // A head synchronization remains safe when browser storage is unavailable.
    }
    return startResumableEventFeed({
      initialCursor,
      onEvents: () => void refresh(),
      onCursor: (cursor) => {
        try {
          window.sessionStorage.setItem("maais.event_cursor.v2", String(cursor));
        } catch {
          // The in-memory cursor still makes this connection gap-free.
        }
      },
      onStatus: setLiveStatus,
    });
  }, [refresh, selectedId]);

  const activeExperiment = useMemo(
    () => experiments.find((item) => item.experiment.id === selectedId) ?? null,
    [experiments, selectedId],
  );

  async function openDecision(decision: DecisionListItem) {
    await openDecisionId(decision.id);
  }

  async function openDecisionId(decisionId: string) {
    setDrawerOpen(true);
    setSelectedDecision(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      setSelectedDecision(await getDecision(decisionId));
    } catch (reason: unknown) {
      setDetailError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDetailLoading(false);
    }
  }

  async function submitOperatorCommand(draft: OperatorActionDraft, token: string) {
    if (!selectedId) return;
    setCommandBusy(true);
    setCommandError(null);
    try {
      const randomIdentity =
        typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      await requestOperatorCommand(
        selectedId,
        token,
        `mission-control-${randomIdentity}`,
        draft,
      );
      await refresh();
    } catch (reason: unknown) {
      setCommandError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setCommandBusy(false);
    }
  }

  if (!loading && experiments.length === 0) {
    return (
      <main className="boot-state">
        <span className="brand-mark">M</span>
        <h1>No paper experiments yet</h1>
        <p>Prepare and start a paper-live manifest; Mission Control will read it automatically.</p>
        {error && <div className="error-panel">{error}</div>}
      </main>
    );
  }

  const account = overview?.account;
  const runtime = overview?.runtime;
  const decisions = overview?.decisions;
  const operations = overview?.operations;
  const freshness = overview?.freshness;
  const currency = overview?.experiment.currency ?? "USDT";
  const killTone: Tone = runtime?.kill_switch_active ? "bad" : "good";

  return (
    <div className="app-shell">
      <aside className="side-rail">
        <div className="brand-lockup"><span className="brand-mark">M</span><div><strong>MAAIS</strong><span>Paper workstation</span></div></div>
        <nav aria-label="Mission Control sections">
          <a className="nav-link nav-link--active" href="#mission"><span>01</span>Mission Control</a>
          <a className="nav-link" href="#trades"><span>02</span>Trade Ledger</a>
          <a className="nav-link" href="#ledger"><span>03</span>Audit Ledger</a>
          <a className="nav-link" href="#operations"><span>04</span>Operations</a>
          <a className="nav-link" href="#operator-console"><span>05</span>Operator Console</a>
          <a className="nav-link" href="#research"><span>06</span>Research Lab</a>
        </nav>
        <div className="rail-safety">
          <span className="safety-dot" />
          <strong>Paper only</strong>
          <p>Public data. Simulated account. No real-money execution adapter.</p>
        </div>
      </aside>

      <main className="workspace" id="mission">
        <header className="topbar">
          <div>
            <span className="kicker">Single-operator quantitative workstation</span>
            <h1>Mission Control</h1>
          </div>
          <div className="topbar__actions">
            <label className="experiment-picker">
              <span>Experiment</span>
              <select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>
                {experiments.map((item) => (
                  <option key={item.experiment.id} value={item.experiment.id}>{item.experiment.name}</option>
                ))}
              </select>
            </label>
            <button className="refresh-button" type="button" onClick={() => void refresh()}>
              <span className={loading ? "refresh-icon refresh-icon--spinning" : "refresh-icon"}>↻</span>
              Refresh
            </button>
          </div>
        </header>

        {error && <div className="error-panel">Mission Control could not refresh: {error}</div>}

        <section className="safety-banner">
          <div className="safety-banner__primary">
            <span className="paper-chip">Paper live</span>
            <div><strong>Zero real money at risk</strong><span>Keyless public market data → deterministic local paper broker</span></div>
          </div>
          <div className="safety-banner__facts">
            <div><span>Worker</span><Badge value={runtime?.worker_status ?? "unknown"} /></div>
            <div><span>Kill switch</span><Badge value={runtime?.kill_switch_active ? "active" : "clear"} tone={killTone} /></div>
            <div><span>Live updates</span><Badge value={liveStatus} tone={liveStatus === "live" ? "good" : "warn"} /></div>
            <div><span>Last refresh</span><strong>{formatTime(lastUpdated)}</strong></div>
          </div>
        </section>

        <ModelBoundary
          assumptions={
            overview?.experiment.model_assumptions
            ?? activeExperiment?.experiment.model_assumptions
            ?? null
          }
        />

        <section className="identity-strip">
          <div><span>Run</span><strong>{overview?.experiment.name ?? activeExperiment?.experiment.name ?? "Loading"}</strong></div>
          <div><span>Experiment state</span><Badge value={overview?.experiment.status ?? null} /></div>
          <div><span>Git</span><code title={overview?.experiment.git_sha}>{shortHash(overview?.experiment.git_sha ?? null)}</code></div>
          <div><span>Manifest</span><code title={overview?.experiment.manifest_hash}>{shortHash(overview?.experiment.manifest_hash ?? null)}</code></div>
          <div><span>Schema</span><strong>{overview?.experiment.schema_revision ?? "—"}</strong></div>
          <div><span>Account source</span><strong>{label(account?.source)}</strong></div>
        </section>

        <section className="dashboard-section">
          <SectionHeader title="Paper account" subtitle="Official values only; counterfactuals never enter these totals" />
          <div className="metric-grid metric-grid--account">
            <MetricCard eyebrow="Equity" value={formatMoney(account?.equity ?? 0, currency)} note={`Peak ${formatMoney(account?.peak_equity ?? 0, currency)}`} tone="good" />
            <MetricCard eyebrow="Realized P&L" value={formatMoney(account?.realized_pnl ?? 0, currency)} note={`Fees ${formatMoney(account?.fees ?? 0, currency)}`} />
            <MetricCard eyebrow="Unrealized P&L" value={formatMoney(account?.unrealized_pnl ?? 0, currency)} note={`Funding ${formatMoney(account?.funding ?? 0, currency)}`} />
            <MetricCard eyebrow="Drawdown" value={formatPercent(account?.drawdown ?? 0)} note={`Risk at stop ${formatMoney(account?.risk_at_stop ?? 0, currency)}`} tone={Number(account?.drawdown ?? 0) > 0.1 ? "bad" : "muted"} />
            <MetricCard eyebrow="Gross exposure" value={formatMoney(account?.gross_notional ?? 0, currency)} note={`Margin used ${formatMoney(account?.used_margin ?? 0, currency)}`} />
            <MetricCard eyebrow="Free margin" value={formatMoney(account?.free_margin ?? 0, currency)} note={`${operations?.open_positions ?? 0} open positions`} />
          </div>
        </section>

        <section className="two-column" id="operations">
          <div className="dashboard-section panel">
            <SectionHeader title="Runtime health" subtitle="Durable worker ownership, data coverage, and controls" />
            <div className="status-list">
              <div><span><i className={`status-dot status-dot--${statusTone(runtime?.worker_status ?? null)}`} />Worker checkpoint</span><div><Badge value={runtime?.worker_status ?? null} /><small>{formatTime(runtime?.checkpoint_at ?? null)}</small></div></div>
              <div><span><i className={`status-dot status-dot--${statusTone(runtime?.lease_status ?? null)}`} />Worker lease</span><div><Badge value={runtime?.lease_status ?? null} /><small>epoch {runtime?.lease_epoch ?? "—"}</small></div></div>
              <div><span><i className={`status-dot status-dot--${killTone}`} />Kill switch</span><div><Badge value={runtime?.kill_switch_active ? "active" : "clear"} tone={killTone} /><small>{runtime?.kill_switch_reason ?? "No active halt"}</small></div></div>
              <div><span><i className={`status-dot status-dot--${freshness?.halted_cursors ? "bad" : "good"}`} />Market cursors</span><div><strong>{freshness?.cursor_count ?? 0} / {freshness?.expected_symbols ?? 0}</strong><small>latest close {formatTime(freshness?.latest_bar_close_at ?? null)}</small></div></div>
              <div><span><i className={`status-dot status-dot--${freshness?.active_recoveries ? "warn" : "good"}`} />Gap recovery</span><div><strong>{freshness?.active_recoveries ?? 0} active</strong><small>{freshness?.halted_cursors ?? 0} halted cursors</small></div></div>
            </div>
          </div>

          <div className="dashboard-section panel">
            <SectionHeader title="Decision throughput" subtitle="One durable cycle per symbol, minute, and strategy" />
            <div className="throughput-grid">
              <div><strong>{formatCompact(decisions?.total ?? 0)}</strong><span>Total cycles</span></div>
              <div><strong>{formatCompact(decisions?.approved ?? 0)}</strong><span>Approved</span></div>
              <div><strong>{formatCompact(decisions?.directional_rejected ?? 0)}</strong><span>Rejected</span></div>
              <div><strong>{formatCompact(decisions?.neutral ?? 0)}</strong><span>Neutral</span></div>
              <div><strong>{formatCompact(decisions?.quarantined ?? 0)}</strong><span>Quarantined</span></div>
              <div><strong>{formatCompact(operations?.fills ?? 0)}</strong><span>Fills</span></div>
            </div>
            <div className={`incident-summary ${operations?.review_incidents ? "incident-summary--attention" : ""}`}>
              <span>{operations?.open_incidents ?? 0} open incidents</span>
              <strong>{operations?.review_incidents ?? 0} need review</strong>
            </div>
          </div>
        </section>

        {overview?.incidents.length ? (
          <section className="dashboard-section">
            <SectionHeader title="Open incidents" subtitle="Persisted operational exceptions requiring visibility" aside={<Badge value={`${overview.incidents.length} open`} tone="warn" />} />
            <div className="incident-grid">
              {overview.incidents.slice(0, 10).map((incident) => (
                <article key={recordString(incident, "id")}>
                  <div><Badge value={recordString(incident, "severity")} /><time>{formatTime(recordString(incident, "detected_at"))}</time></div>
                  <strong>{label(recordString(incident, "reason_code"))}</strong>
                  <span>{label(recordString(incident, "component"))} · {label(recordString(incident, "status"))}</span>
                  <JsonDetails labelText="Incident evidence" value={recordValue(incident, "evidence_json")} />
                </article>
              ))}
            </div>
          </section>
        ) : null}

        <OperatorConsole
          commands={commands}
          runtime={runtime}
          incidents={overview?.incidents ?? []}
          token={controlToken}
          busy={commandBusy}
          error={commandError}
          onTokenChange={setControlToken}
          onSubmit={(draft, token) => void submitOperatorCommand(draft, token)}
        />

        <section className="dashboard-section" id="trades">
          <SectionHeader
            title="Trade Ledger"
            subtitle="Every directional proposal with linked official fills, costs, and isolated research outcome"
            aside={<span className="freshness-label">Click a symbol for the complete decision bundle</span>}
          />
          {loading && !tradePage ? <div className="table-loading">Loading proposed trades…</div> : <TradeTable page={tradePage} currency={currency} onOpen={(decisionId) => void openDecisionId(decisionId)} />}
        </section>

        <ResearchLab
          research={research}
          currency={currency}
          onOpen={(decisionId) => void openDecisionId(decisionId)}
        />

        <section className="dashboard-section" id="ledger">
          <SectionHeader
            title="Audit Ledger"
            subtitle="Every neutral, rejected, quarantined, approved, and filled decision"
            aside={<span className="freshness-label">Updated {formatTime(lastUpdated)}</span>}
          />
          <div className="filter-bar">
            <label><span>Symbol</span><input value={filters.symbol} onChange={(event) => setFilters((current) => ({ ...current, symbol: event.target.value }))} placeholder="All symbols" /></label>
            <label><span>Status</span><select value={filters.status} onChange={(event) => setFilters((current) => ({ ...current, status: event.target.value }))}><option value="">All statuses</option><option value="completed">Completed</option><option value="rejected">Rejected</option><option value="quarantined">Quarantined</option></select></label>
            <label><span>Disposition</span><select value={filters.disposition} onChange={(event) => setFilters((current) => ({ ...current, disposition: event.target.value }))}><option value="">All outcomes</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="neutral">Neutral</option></select></label>
            <label className="filter-reason"><span>Reason code</span><input value={filters.reasonCode} onChange={(event) => setFilters((current) => ({ ...current, reasonCode: event.target.value }))} placeholder="e.g. data_quality_failed" /></label>
            <button type="button" className="clear-button" onClick={() => setFilters(EMPTY_FILTERS)}>Clear</button>
          </div>
          {loading && !decisionPage ? <div className="table-loading">Loading decision ledger…</div> : <DecisionTable page={decisionPage} onOpen={(decision) => void openDecision(decision)} />}
        </section>

        <footer>
          <span>MAAIS Mission Control · local operator surface</span>
          <span>All displayed values read from immutable manifests or PostgreSQL projections.</span>
        </footer>
      </main>

      {drawerOpen && (
        <DecisionDrawer detail={selectedDecision} loading={detailLoading} error={detailError} onClose={() => setDrawerOpen(false)} />
      )}
    </div>
  );
}
