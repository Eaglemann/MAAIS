import { useMemo, useState } from "react";

import { formatTime, label, shortHash } from "./format";
import type {
  JsonRecord,
  OperatorActionDraft,
  OperatorCommandPage,
  OperatorCommandType,
  RuntimeOverview,
} from "./types";

const ACTIONS: ReadonlyArray<{ command: OperatorCommandType; text: string; danger?: boolean }> = [
  { command: "start", text: "Start worker" },
  { command: "pause", text: "Pause worker" },
  { command: "resume", text: "Resume worker" },
  { command: "stop", text: "Stop worker", danger: true },
  { command: "emergency_halt", text: "Emergency halt", danger: true },
  { command: "flatten", text: "Flatten positions", danger: true },
  { command: "reset_kill_switch", text: "Reset kill switch", danger: true },
];

function recordString(record: JsonRecord, key: string): string {
  return String(record[key] ?? "—");
}

function exactPhrase(command: OperatorCommandType): string {
  return `CONFIRM ${command.toUpperCase()}`;
}

export function OperatorConsole({
  commands,
  runtime,
  incidents,
  token,
  busy,
  error,
  onTokenChange,
  onSubmit,
}: {
  commands: OperatorCommandPage | null;
  runtime: RuntimeOverview | null | undefined;
  incidents: JsonRecord[];
  token: string;
  busy: boolean;
  error: string | null;
  onTokenChange: (token: string) => void;
  onSubmit: (draft: OperatorActionDraft, token: string) => void;
}) {
  const [selected, setSelected] = useState<OperatorCommandType | null>(null);
  const [incidentId, setIncidentId] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [resolution, setResolution] = useState("");
  const [confirmation, setConfirmation] = useState("");

  const requiredPhrase = selected ? exactPhrase(selected) : "";
  const payload = useMemo<JsonRecord>(() => {
    if (selected === "resume") {
      return { expected_control_version: runtime?.control_version ?? null };
    }
    if (selected === "reset_kill_switch") {
      return {
        expected_control_version: runtime?.control_version ?? null,
        expected_reason: runtime?.kill_switch_reason ?? null,
      };
    }
    if (selected === "acknowledge_incident") return { incident_id: incidentId };
    if (selected === "resolve_incident") {
      return { incident_id: incidentId, resolution: resolution.trim() };
    }
    return {};
  }, [incidentId, resolution, runtime, selected]);

  const payloadReady =
    selected !== "resume" && selected !== "reset_kill_switch"
      ? selected !== "resolve_incident" || Boolean(incidentId && resolution.trim())
      : runtime?.control_version !== null &&
        runtime?.control_version !== undefined &&
        (selected !== "reset_kill_switch" || Boolean(runtime.kill_switch_reason));
  const canSubmit = Boolean(
    selected &&
      token.trim() &&
      reason.trim() &&
      confirmation === requiredPhrase &&
      payloadReady &&
      !busy,
  );

  function choose(command: OperatorCommandType, selectedIncidentId: string | null = null) {
    setSelected(command);
    setIncidentId(selectedIncidentId);
    setReason("");
    setResolution("");
    setConfirmation("");
  }

  function submit() {
    if (!selected || !canSubmit) return;
    onSubmit(
      {
        commandType: selected,
        reason: reason.trim(),
        payload,
        confirmation,
      },
      token,
    );
  }

  return (
    <section className="dashboard-section" id="operator-console">
      <div className="section-header">
        <div>
          <h2>Operator Console</h2>
          <p>Audited, queued controls executed by the paper worker—not by the browser.</p>
        </div>
        <span className="paper-chip">Local paper only</span>
      </div>

      <div className="operator-layout">
        <div className="panel operator-controls">
          <label className="operator-token">
            <span>Local control token</span>
            <input
              aria-label="Local control token"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(event) => onTokenChange(event.target.value)}
              placeholder="Loaded from the private runtime token file"
            />
            <small>Kept in this tab only. It is never written to logs or the database.</small>
          </label>
          <div className="operator-action-grid">
            {ACTIONS.map((action) => (
              <button
                key={action.command}
                type="button"
                className={`operator-action ${action.danger ? "operator-action--danger" : ""}`}
                onClick={() => choose(action.command)}
              >
                {action.text}
              </button>
            ))}
          </div>

          {incidents.length > 0 && (
            <div className="incident-actions">
              <strong>Incident actions</strong>
              {incidents.map((incident) => {
                const id = recordString(incident, "id");
                return (
                  <article key={id}>
                    <div>
                      <span>{label(recordString(incident, "reason_code"))}</span>
                      <small>{label(recordString(incident, "status"))} · {id}</small>
                    </div>
                    <button type="button" onClick={() => choose("acknowledge_incident", id)}>
                      Acknowledge incident
                    </button>
                    <button type="button" onClick={() => choose("resolve_incident", id)}>
                      Resolve incident
                    </button>
                  </article>
                );
              })}
            </div>
          )}

          {selected && (
            <div className="command-confirmation">
              <div>
                <span className="kicker">Pending operator request</span>
                <h3>{label(selected)}</h3>
              </div>
              {(selected === "resume" || selected === "reset_kill_switch") && (
                <p className="control-lineage">
                  Expected control version {runtime?.control_version ?? "unavailable"}
                  {selected === "reset_kill_switch" && (
                    <> · exact reason <code>{runtime?.kill_switch_reason ?? "unavailable"}</code></>
                  )}
                </p>
              )}
              <label>
                <span>Operator reason</span>
                <input
                  aria-label="Operator reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Why is this action necessary?"
                />
              </label>
              {selected === "resolve_incident" && (
                <label>
                  <span>Reviewed resolution</span>
                  <input
                    aria-label="Reviewed resolution"
                    value={resolution}
                    onChange={(event) => setResolution(event.target.value)}
                    placeholder="Evidence-backed resolution"
                  />
                </label>
              )}
              <label>
                <span>Exact confirmation phrase</span>
                <input
                  aria-label="Exact confirmation phrase"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  placeholder={requiredPhrase}
                />
              </label>
              <code className="confirmation-phrase">{requiredPhrase}</code>
              <button
                type="button"
                className="queue-command"
                disabled={!canSubmit}
                onClick={submit}
              >
                {busy ? "Queueing…" : "Queue confirmed command"}
              </button>
              {error && <div className="error-panel">Command not queued: {error}</div>}
            </div>
          )}
        </div>

        <div className="panel command-history">
          <div className="section-header">
            <div><h2>Command history</h2><p>Request, worker acceptance, terminal result, and hashes.</p></div>
            <span className="freshness-label">{commands?.items.length ?? 0} shown</span>
          </div>
          {!commands?.items.length ? (
            <div className="empty-inline">No operator commands have been requested.</div>
          ) : (
            <ol>
              {commands.items.map((command) => (
                <li key={command.command_id}>
                  <div className="command-history__title">
                    <strong>{label(command.command_type)}</strong>
                    <span className={`badge badge--${command.status === "rejected" ? "bad" : command.status === "completed" ? "good" : "warn"}`}>{label(command.status)}</span>
                  </div>
                  <p>{command.reason}</p>
                  <dl>
                    <div><dt>Requested</dt><dd>{formatTime(command.requested_at)}</dd></div>
                    <div><dt>Accepted</dt><dd>{command.accepted_by ?? "Awaiting worker"} · {formatTime(command.accepted_at)}</dd></div>
                    <div><dt>Completed</dt><dd>{formatTime(command.completed_at)}</dd></div>
                    <div><dt>Request hash</dt><dd><code title={command.request_hash}>{shortHash(command.request_hash)}</code></dd></div>
                  </dl>
                  <details className="json-details">
                    <summary>Payload and terminal result</summary>
                    <pre>{JSON.stringify({ payload: command.payload, result: command.result }, null, 2)}</pre>
                  </details>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </section>
  );
}
