import { formatMoney, formatTime, label, shortHash } from "./format";
import type { ResearchLabView } from "./types";

export function ResearchLab({
  research,
  currency,
  onOpen,
}: {
  research: ResearchLabView | null;
  currency: string;
  onOpen: (decisionId: string) => void;
}) {
  const counterfactuals = research?.counterfactuals ?? [];
  const sensitivities = research?.execution_sensitivities ?? [];
  return (
    <section className="dashboard-section" id="research">
      <div className="section-header">
        <div>
          <h2>Research Lab</h2>
          <p>Hypotheses and execution stress tests are excluded from official account P&amp;L.</p>
        </div>
        <span className="research-isolation">Research only · structurally isolated</span>
      </div>
      <div className="research-grid">
        <div className="panel research-panel">
          <div className="research-panel__header">
            <div><strong>Rejected-trade counterfactuals</strong><span>{counterfactuals.length} tracked</span></div>
          </div>
          {counterfactuals.length === 0 ? (
            <div className="empty-inline">No rejected directional proposal has a research path yet.</div>
          ) : (
            <div className="research-card-list">
              {counterfactuals.map((item) => (
                <article key={item.id}>
                  <div className="research-card__title">
                    <button
                      type="button"
                      className="row-link"
                      aria-label={`Inspect ${item.symbol} counterfactual`}
                      onClick={() => onOpen(item.decision_cycle_id)}
                    >
                      {item.symbol}
                    </button>
                    <span className={`badge badge--${item.status === "resolved" ? "good" : "info"}`}>{label(item.status)}</span>
                  </div>
                  <span>{label(item.direction)} · rejected at {label(item.rejection_gate)}</span>
                  <dl>
                    <div><dt>Hypothetical P&amp;L</dt><dd>{item.hypothetical_pnl === null ? "Pending" : formatMoney(item.hypothetical_pnl, currency)}</dd></div>
                    <div><dt>15m / 1h</dt><dd>{item.outcome_15m ?? "—"} / {item.outcome_1h ?? "—"}</dd></div>
                    <div><dt>4h / 24h</dt><dd>{item.outcome_4h ?? "—"} / {item.outcome_24h ?? "—"}</dd></div>
                    <div><dt>MFE / MAE</dt><dd>{item.maximum_favorable_excursion} / {item.maximum_adverse_excursion}</dd></div>
                  </dl>
                  <small>{formatTime(item.created_at)} · <code title={item.content_hash}>{shortHash(item.content_hash)}</code></small>
                </article>
              ))}
            </div>
          )}
        </div>

        <div className="panel research-panel">
          <div className="research-panel__header">
            <div><strong>Execution sensitivities</strong><span>{sensitivities.length} scenarios</span></div>
          </div>
          {sensitivities.length === 0 ? (
            <div className="empty-inline">No official paper fill has sensitivity scenarios yet.</div>
          ) : (
            <div className="research-card-list">
              {sensitivities.map((item) => (
                <article key={item.id}>
                  <div className="research-card__title">
                    <strong>{item.symbol}</strong>
                    <span className={`badge badge--${item.scenario === "stress" ? "warn" : item.scenario === "optimistic" ? "good" : "info"}`}>{label(item.scenario)}</span>
                  </div>
                  <dl>
                    <div><dt>Marked P&amp;L</dt><dd>{formatMoney(String(item.outcome.marked_pnl ?? 0), currency)}</dd></div>
                    <div><dt>Execution cost</dt><dd>{formatMoney(String(item.outcome.execution_cost ?? 0), currency)}</dd></div>
                    <div><dt>Effective fill</dt><dd>{String(item.outcome.effective_fill_price ?? "—")}</dd></div>
                    <div><dt>Calculated</dt><dd>{formatTime(item.calculated_at)}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
