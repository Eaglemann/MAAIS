import { formatTime, label, shortHash, type Tone } from "./format";
import type {
  CloudEvidencePageKind,
  CloudOperationsEvidence,
  CloudStoredArtifactView,
} from "./types";

const REQUIRED_ROLES = ["worker", "web", "operations"] as const;
const REPLICATION_CHECKS = new Set(["backup", "daily_close", "worm_replication"]);
const HEALTH_FRESHNESS_MS = 180_000;

interface CloudState {
  label: string;
  note: string;
  tone: Tone;
}

export interface CloudOperationsProps {
  evidence: CloudOperationsEvidence | null;
  loading: boolean;
  error: string | null;
  fillCount: number;
  rationaleComplete: boolean;
  now?: Date;
  onLoadOlder: (kind: CloudEvidencePageKind) => void;
}

function Badge({ value, tone }: { value: string; tone: Tone }) {
  return <span className={`badge badge--${tone}`}>{label(value)}</span>;
}

function statesFor(
  evidence: CloudOperationsEvidence,
  fillCount: number,
  rationaleComplete: boolean,
  now: Date,
): CloudState[] {
  const { run, services, health } = evidence;
  const latestHealth = health.items[0] ?? null;
  const checkedAt = latestHealth ? new Date(latestHealth.checked_at).getTime() : Number.NaN;
  const staleHealth = !Number.isFinite(checkedAt)
    || now.getTime() - checkedAt > HEALTH_FRESHNESS_MS;
  const failedReplication = latestHealth?.failed_check_names.some((name) => (
    REPLICATION_CHECKS.has(name)
  )) ?? false;
  const activeServices = new Map<string, (typeof services.items)[number]>();
  for (const service of services.items) {
    if (service.stopped_at === null && !activeServices.has(service.service_role)) {
      activeServices.set(service.service_role, service);
    }
  }
  const worker = activeServices.get("worker");
  const interrupted = run.status !== "standby" && (
    REQUIRED_ROLES.some((role) => !activeServices.has(role))
    || worker?.boot_id !== run.activating_worker_boot_id
  );
  const runState: CloudState = run.status === "standby"
    ? { label: "Standby", note: "Awaiting an audited operator start command.", tone: "info" }
    : run.status === "active"
      ? { label: "Active", note: "Official paper run is active.", tone: "good" }
      : run.status === "completed"
        ? { label: "Completed", note: "Run reached a recorded terminal state.", tone: "good" }
        : { label: "Invalidated", note: run.invalidation_reason ?? "Continuity was invalidated.", tone: "bad" };

  return [
    runState,
    run.continuity_invalidated
      ? {
        label: "Continuity invalidated",
        note: run.invalidation_reason ?? "The immutable run continuity gate failed.",
        tone: "bad",
      }
      : { label: "Continuity intact", note: "No durable invalidation is recorded.", tone: "good" },
    interrupted
      ? { label: "Interrupted", note: "A required exact service boot is absent or terminal.", tone: "bad" }
      : {
        label: "Required boots continuous",
        note: run.status === "standby"
          ? "Boot continuity begins only after activation."
          : "Worker, web, and operations identities match the active run.",
        tone: run.status === "standby" ? "info" : "good",
      },
    latestHealth === null
      ? {
        label: "Evidence replication unverified",
        note: "No immutable health evaluation is available yet.",
        tone: "warn",
      }
      : failedReplication
      ? {
        label: "Failed evidence replication",
        note: latestHealth?.failed_check_names.join(", ") ?? "Replication check failed.",
        tone: "bad",
      }
      : {
        label: "Evidence replication verified",
        note: "Latest immutable health evidence records no replication failure.",
        tone: "good",
      },
    staleHealth
      ? { label: "Stale health", note: "No verified health snapshot exists within three minutes.", tone: "bad" }
      : {
        label: "Health current",
        note: `Verified ${formatTime(latestHealth?.checked_at ?? null)}.`,
        tone: latestHealth?.overall_status === "healthy" ? "good" : "warn",
      },
    fillCount === 0
      ? {
        label: "No fills yet",
        note: "Informational; thresholds stay unchanged.",
        tone: "info",
      }
      : { label: `${fillCount} paper fills`, note: "Official simulated fills only.", tone: "info" },
    rationaleComplete
      ? {
        label: "Decision rationale complete",
        note: "Visible decisions include their stored reason metadata.",
        tone: "good",
      }
      : {
        label: "Incomplete decision rationale",
        note: "At least one visible decision lacks required rationale metadata.",
        tone: "bad",
      },
  ];
}

function Inventory({ item, canonical }: { item: CloudStoredArtifactView; canonical: boolean }) {
  return (
    <div className="cloud-inventory">
      <div>
        <strong>{item.store_name}</strong>
        <Badge value={canonical ? "canonical" : "replica"} tone={canonical ? "good" : "info"} />
      </div>
      <code title={item.key}>{item.key}</code>
      <dl>
        <div><dt>ETag</dt><dd>{item.etag}</dd></div>
        <div><dt>Version</dt><dd>{item.version_id ?? "not versioned"}</dd></div>
        {canonical && <div><dt>Retention</dt><dd>{item.retention_mode}</dd></div>}
        {canonical && <div><dt>Retain until</dt><dd>{formatTime(item.retain_until)}</dd></div>}
      </dl>
    </div>
  );
}

function LoadOlder({
  kind,
  visible,
  onLoadOlder,
}: {
  kind: CloudEvidencePageKind;
  visible: boolean;
  onLoadOlder: (kind: CloudEvidencePageKind) => void;
}) {
  if (!visible) return null;
  return (
    <button
      className="cloud-load-older"
      type="button"
      aria-label={`Load older ${kind} evidence`}
      onClick={() => onLoadOlder(kind)}
    >
      Load older
    </button>
  );
}

export function CloudOperations({
  evidence,
  loading,
  error,
  fillCount,
  rationaleComplete,
  now = new Date(),
  onLoadOlder,
}: CloudOperationsProps) {
  return (
    <section className="dashboard-section cloud-operations" id="cloud-operations">
      <div className="section-header">
        <div>
          <h2>Operations evidence</h2>
          <p>Authenticated, hash-verified cloud identity, continuity, health, and immutable evidence.</p>
        </div>
        <div className="cloud-evidence-links" aria-label="Evidence links">
          <a href="#trades">Trade Ledger</a>
          <a href="#ledger">Decision rationale</a>
          <a href="#research">Research Lab</a>
        </div>
      </div>

      {loading && !evidence && <div className="table-loading">Loading verified operations evidence…</div>}
      {error && <div className="error-panel">Operations evidence could not refresh: {error}</div>}
      {!loading && !error && !evidence && (
        <div className="empty-inline">No cloud run is registered for this experiment.</div>
      )}
      {evidence && (
        <>
          <div className="cloud-identity-grid">
            <div><span>Candidate</span><code title={evidence.candidate.descriptor_hash}>{shortHash(evidence.candidate.descriptor_hash)}</code></div>
            <div><span>Run</span><code title={evidence.run.id}>{evidence.run.id}</code></div>
            <div><span>Database cluster</span><code>{evidence.run.database_system_identifier}</code></div>
            <div><span>Railway environment</span><code>{evidence.run.railway_environment_id}</code></div>
            <div><span>Manifest</span><code title={evidence.run.manifest_hash}>{shortHash(evidence.run.manifest_hash)}</code></div>
            <div><span>Schema</span><strong>{evidence.candidate.schema_revision}</strong></div>
            <div><span>Git SHA</span><code title={evidence.candidate.git_sha}>{shortHash(evidence.candidate.git_sha)}</code></div>
            <div><span>Run status</span><code>{evidence.run.status}</code></div>
          </div>

          <div className="cloud-state-grid" aria-label="Cloud evidence states">
            {statesFor(evidence, fillCount, rationaleComplete, now).map((state) => (
              <article className={`cloud-state cloud-state--${state.tone}`} key={state.label}>
                <strong>{state.label}</strong>
                <span>{state.note}</span>
              </article>
            ))}
          </div>

          <div className="cloud-evidence-grid">
            <section className="panel cloud-evidence-panel cloud-evidence-panel--wide">
              <header><div><strong>Required service boots</strong><span>Exact deployment, replica, region, heartbeat, and terminal state</span></div></header>
              <div className="cloud-service-list">
                {evidence.services.items.length === 0 && (
                  <div className="empty-inline">No service boot evidence is recorded.</div>
                )}
                {evidence.services.items.map((service) => (
                  <article key={service.boot_id}>
                    <div className="cloud-card-title">
                      <div><strong>{label(service.service_role)}</strong><span>{service.service_id}</span></div>
                      <Badge value={service.stopped_at ? "stopped" : "running"} tone={service.stopped_at ? "bad" : "good"} />
                    </div>
                    <dl>
                      <div><dt>Boot</dt><dd>{service.boot_id}</dd></div>
                      <div><dt>Deployment</dt><dd>{service.deployment_id}</dd></div>
                      <div><dt>Replica</dt><dd>{service.replica_id}</dd></div>
                      <div><dt>Region</dt><dd>{service.region}</dd></div>
                      <div><dt>Heartbeat</dt><dd>{formatTime(service.last_heartbeat_at)}</dd></div>
                      <div><dt>Sequence</dt><dd>{service.heartbeat_sequence}</dd></div>
                    </dl>
                    {service.terminal_reason && <small>Terminal: {label(service.terminal_reason)}</small>}
                  </article>
                ))}
              </div>
              <LoadOlder kind="services" visible={evidence.services.has_more} onLoadOlder={onLoadOlder} />
            </section>

            <section className="panel cloud-evidence-panel">
              <header><div><strong>Minute health history</strong><span>Immutable evaluations with complete component evidence</span></div></header>
              <div className="cloud-timeline">
                {evidence.health.items.length === 0 && (
                  <div className="empty-inline">No immutable health evaluations are recorded.</div>
                )}
                {evidence.health.items.map((item) => (
                  <article key={item.evaluation_id}>
                    <div className="cloud-card-title">
                      <time>{formatTime(item.checked_at)}</time>
                      <Badge value={item.overall_status} tone={item.overall_status === "healthy" ? "good" : "bad"} />
                    </div>
                    <strong>{item.failed_check_names.length ? item.failed_check_names.join(", ") : "All checks passed"}</strong>
                    <code title={item.content_hash}>{shortHash(item.content_hash)}</code>
                    <details><summary>Component evidence</summary><pre>{JSON.stringify(item.components, null, 2)}</pre></details>
                  </article>
                ))}
              </div>
              <LoadOlder kind="health" visible={evidence.health.has_more} onLoadOlder={onLoadOlder} />
            </section>

            <section className="panel cloud-evidence-panel">
              <header><div><strong>Incidents</strong><span>Open, reviewed, recovered, and terminal operational episodes</span></div></header>
              {evidence.incidents.items.length === 0 ? (
                <div className="empty-inline">No cloud incidents are recorded.</div>
              ) : (
                <div className="cloud-timeline">
                  {evidence.incidents.items.map((incident) => (
                    <article key={incident.id}>
                      <div className="cloud-card-title">
                        <time>{formatTime(incident.detected_at)}</time>
                        <Badge value={incident.status} tone={incident.status === "resolved" ? "good" : "warn"} />
                      </div>
                      <strong>{label(incident.reason_code)}</strong>
                      <span>{label(incident.component)} · {label(incident.severity)}</span>
                    </article>
                  ))}
                </div>
              )}
              <LoadOlder kind="incidents" visible={evidence.incidents.has_more} onLoadOlder={onLoadOlder} />
            </section>

            <section className="panel cloud-evidence-panel cloud-evidence-panel--wide">
              <header><div><strong>Artifact replication</strong><span>Replica and canonical WORM target, exact version, and retention</span></div></header>
              <div className="cloud-artifact-list">
                {evidence.artifacts.items.length === 0 && (
                  <div className="empty-inline">No immutable artifacts are published yet.</div>
                )}
                {evidence.artifacts.items.map((artifact) => (
                  <article key={artifact.id}>
                    <div className="cloud-card-title">
                      <div><strong>{label(artifact.artifact_type)}</strong><span>{artifact.report_id}</span></div>
                      <code title={artifact.catalog_content_hash}>{shortHash(artifact.catalog_content_hash)}</code>
                    </div>
                    <div className="cloud-inventory-grid">
                      {artifact.replica_inventory.map((item) => <Inventory item={item} canonical={false} key={`${item.store_name}:${item.key}`} />)}
                      {artifact.canonical_inventory.map((item) => <Inventory item={item} canonical key={`${item.store_name}:${item.key}`} />)}
                    </div>
                  </article>
                ))}
              </div>
              <LoadOlder kind="artifacts" visible={evidence.artifacts.has_more} onLoadOlder={onLoadOlder} />
            </section>

            <section className="panel cloud-evidence-panel cloud-evidence-panel--wide">
              <header><div><strong>Audit timeline</strong><span>Globally chained events filtered to this exact run</span></div></header>
              <div className="cloud-audit-list">
                {evidence.audit.items.length === 0 && (
                  <div className="empty-inline">No run-scoped audit events are recorded.</div>
                )}
                {evidence.audit.items.map((event) => (
                  <article key={event.sequence}>
                    <span>{event.sequence}</span>
                    <div><strong>{label(event.event_code)}</strong><small>{label(event.reason_code)} · {label(event.source_role)}</small></div>
                    <time>{formatTime(event.occurred_at)}</time>
                    <code title={event.content_hash}>{shortHash(event.content_hash)}</code>
                  </article>
                ))}
              </div>
              <LoadOlder kind="audit" visible={evidence.audit.has_more} onLoadOlder={onLoadOlder} />
            </section>
          </div>

          <p className="cloud-provider-note">
            Routine logs stay in Railway and exceptions stay in Sentry. Use the exact deployment,
            replica, boot, release, and candidate identities above when following provider context;
            provider credentials and raw exception payloads are never returned here.
          </p>
        </>
      )}
    </section>
  );
}
