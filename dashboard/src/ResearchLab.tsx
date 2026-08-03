import { formatMoney, formatPercent, formatTime, label, shortHash } from "./format";
import type { ResearchLabView } from "./types";

function optionalMoney(value: string | null, currency: string): string {
  return value === null ? "Unavailable" : formatMoney(value, currency);
}

function optionalNumber(value: string | null, suffix = ""): string {
  if (value === null) return "Unavailable";
  const parsed = Number(value);
  return `${Number.isFinite(parsed) ? parsed.toFixed(2) : "—"}${suffix}`;
}

function EquityChart({ points }: { points: ResearchLabView["equity_curve"] }) {
  if (points.length < 2) {
    return <div className="empty-inline">The equity curve appears after two account snapshots.</div>;
  }
  const values = points.map((point) => Number(point.equity));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const polyline = values
    .map((value, index) => `${(index / (values.length - 1)) * 100},${38 - ((value - low) / span) * 34}`)
    .join(" ");
  return (
    <div className="equity-chart">
      <svg viewBox="0 0 100 42" role="img" aria-label="Official paper account equity curve">
        <polyline points={polyline} fill="none" vectorEffect="non-scaling-stroke" />
      </svg>
      <div><span>{formatMoney(String(low))}</span><span>{formatMoney(String(high))}</span></div>
    </div>
  );
}

function AttributionTable({
  title,
  rows,
  currency,
}: {
  title: string;
  rows: ResearchLabView["attribution"][string];
  currency: string;
}) {
  return (
    <details className="research-details" open={title === "Symbol"}>
      <summary>{title}<span>{rows.length} groups</span></summary>
      {rows.length === 0 ? <div className="empty-inline">No closed-trade observations.</div> : (
        <div className="table-shell">
          <table>
            <thead><tr><th>Group</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Expectancy</th></tr></thead>
            <tbody>{rows.map((row) => (
              <tr key={row.key}>
                <td>{row.key}</td><td>{row.trades}</td><td>{formatPercent(row.win_rate)}</td>
                <td>{formatMoney(row.net_pnl_ex_funding, currency)}</td>
                <td>{formatMoney(row.expectancy, currency)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </details>
  );
}

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
  const performance = research?.performance;
  const consensus = research?.calibration?.consensus;
  const buyAndHold = research?.benchmarks?.buy_and_hold;
  const flatCash = research?.benchmarks?.flat_cash;
  return (
    <section className="dashboard-section" id="research">
      <div className="section-header">
        <div>
          <h2>Research Lab</h2>
          <p>Hypotheses and execution stress tests are excluded from official account P&amp;L.</p>
        </div>
        <span className="research-isolation">Research only · structurally isolated</span>
      </div>
      {research && performance ? (
        <>
          <div className="metric-grid research-metric-grid">
            <article className={`metric-card metric-card--${Number(research.cost_waterfall.net_change) >= 0 ? "good" : "bad"}`}>
              <span className="metric-card__eyebrow">Official net change</span>
              <strong>{formatMoney(research.cost_waterfall.net_change, currency)}</strong>
              <span className="metric-card__note">Account-level, fully reconciled</span>
            </article>
            <article className="metric-card">
              <span className="metric-card__eyebrow">Win rate</span>
              <strong>{performance.win_rate === null ? "Unavailable" : formatPercent(performance.win_rate)}</strong>
              <span className="metric-card__note">{performance.closed_trade_allocations} FIFO close allocations</span>
            </article>
            <article className="metric-card">
              <span className="metric-card__eyebrow">Expectancy</span>
              <strong>{optionalMoney(performance.expectancy, currency)}</strong>
              <span className="metric-card__note">Net of opening and closing fees</span>
            </article>
            <article className="metric-card">
              <span className="metric-card__eyebrow">Profit factor</span>
              <strong>{optionalNumber(performance.profit_factor)}</strong>
              <span className="metric-card__note">Unavailable until at least one loss</span>
            </article>
            <article className="metric-card">
              <span className="metric-card__eyebrow">Average R</span>
              <strong>{optionalNumber(performance.average_r_multiple, "R")}</strong>
              <span className="metric-card__note">Uses proposal risk at stop</span>
            </article>
            <article className="metric-card">
              <span className="metric-card__eyebrow">Consensus calibration</span>
              <strong>{consensus?.brier_score === null || consensus?.brier_score === undefined ? "Unavailable" : Number(consensus.brier_score).toFixed(3)}</strong>
              <span className="metric-card__note">Brier score · {consensus?.sample_size ?? 0} resolved outcomes</span>
            </article>
          </div>

          <div className="research-grid research-analytics-grid">
            <div className="panel research-panel">
              <div className="research-panel__header"><div><strong>Equity and drawdown</strong><span>As of {formatTime(research.analytics_as_of)}</span></div></div>
              <EquityChart points={research.equity_curve} />
              <dl className="mini-metrics">
                <div><dt>Start</dt><dd>{formatMoney(research.cost_waterfall.initial_capital, currency)}</dd></div>
                <div><dt>End</dt><dd>{formatMoney(research.cost_waterfall.ending_equity, currency)}</dd></div>
                <div><dt>Max drawdown</dt><dd>{formatPercent(String(Math.max(0, ...research.equity_curve.map((point) => Number(point.drawdown)))))}</dd></div>
                <div><dt>Identity</dt><dd>{research.cost_waterfall.reconciles ? "Reconciled" : "Mismatch"}</dd></div>
              </dl>
            </div>

            <div className="panel research-panel">
              <div className="research-panel__header"><div><strong>Gross-to-net waterfall</strong><span>Official account identity</span></div></div>
              <dl className="research-waterfall">
                <div><dt>Gross realized P&amp;L</dt><dd>{formatMoney(research.cost_waterfall.gross_realized_pnl, currency)}</dd></div>
                <div><dt>Fees</dt><dd>{formatMoney(research.cost_waterfall.fees, currency)}</dd></div>
                <div><dt>Funding</dt><dd>{formatMoney(research.cost_waterfall.funding, currency)}</dd></div>
                <div><dt>Unrealized P&amp;L</dt><dd>{formatMoney(research.cost_waterfall.unrealized_pnl, currency)}</dd></div>
                <div className="research-waterfall__total"><dt>Net change</dt><dd>{formatMoney(research.cost_waterfall.net_change, currency)}</dd></div>
              </dl>
              {research.availability.funding_attribution?.status === "unavailable" ? (
                <p className="research-caveat">Funding attribution unavailable — funding remains authoritative in account P&amp;L but is not assigned to individual close fills.</p>
              ) : null}
            </div>

            <div className="panel research-panel">
              <div className="research-panel__header"><div><strong>Explicit benchmarks</strong><span>Same observed period</span></div></div>
              <dl className="research-waterfall">
                <div><dt>Paper strategy</dt><dd>{formatMoney(research.cost_waterfall.ending_equity, currency)}</dd></div>
                <div><dt>Buy and hold</dt><dd>{buyAndHold?.status === "available" ? formatMoney(String(buyAndHold.ending_equity), currency) : "Unavailable"}</dd></div>
                <div><dt>Flat cash</dt><dd>{formatMoney(String(flatCash?.ending_equity ?? 0), currency)}</dd></div>
              </dl>
              <p className="research-caveat">Buy and hold is equal-weight long, first-to-last observed close, with no costs. Flat cash earns zero interest.</p>
            </div>

            <div className="panel research-panel">
              <div className="research-panel__header"><div><strong>Excursion and gate value</strong><span>Resolved observations only</span></div></div>
              <dl className="mini-metrics">
                <div><dt>Max MFE</dt><dd>{optionalMoney(performance.maximum_favorable_excursion, currency)}</dd></div>
                <div><dt>Max MAE</dt><dd>{optionalMoney(performance.maximum_adverse_excursion, currency)}</dd></div>
                <div><dt>Gate samples</dt><dd>{research.gate_value.resolved_sample_size}</dd></div>
                <div><dt>Agents calibrated</dt><dd>{Object.keys(research.calibration).length - 1}</dd></div>
              </dl>
              {research.gate_value.by_gate.map((row) => (
                <div className="research-inline-row" key={row.gate}><span>{label(row.gate)}</span><strong>{formatMoney(row.avoided_pnl, currency)} avoided</strong></div>
              ))}
              {Object.entries(research.cost_sensitivity).map(([scenario, row]) => (
                <div className="research-inline-row" key={scenario}><span>{label(scenario)} cost band</span><strong>{formatMoney(row.marked_pnl, currency)}</strong></div>
              ))}
            </div>
          </div>

          <div className="panel research-attribution">
            <div className="research-panel__header"><div><strong>Performance attribution</strong><span>Official closed-fill results, ex funding</span></div></div>
            {([
              ["Symbol", "by_symbol"], ["Regime", "by_regime"], ["Strategy", "by_strategy"],
              ["Agent coalition", "by_agent_coalition"], ["Entry hour (Berlin)", "by_hour_berlin"],
              ["Direction", "by_direction"], ["Exit reason", "by_exit_reason"],
            ] as const).map(([title, key]) => (
              <AttributionTable key={key} title={title} rows={research.attribution[key] ?? []} currency={currency} />
            ))}
          </div>

          <div className="panel research-calibration">
            <div className="research-panel__header"><div><strong>Probability calibration</strong><span>Lower Brier score is better</span></div></div>
            <div className="table-shell"><table><thead><tr><th>Predictor</th><th>Samples</th><th>Brier score</th><th>Mean probability</th><th>Observed win rate</th></tr></thead>
              <tbody>{Object.entries(research.calibration).map(([name, row]) => (
                <tr key={name}><td>{label(name)}</td><td>{row.sample_size}</td><td>{row.brier_score === null ? "Unavailable" : Number(row.brier_score).toFixed(3)}</td><td>{row.mean_probability === null ? "—" : formatPercent(row.mean_probability)}</td><td>{row.observed_win_rate === null ? "—" : formatPercent(row.observed_win_rate)}</td></tr>
              ))}</tbody>
            </table></div>
          </div>
        </>
      ) : null}
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
