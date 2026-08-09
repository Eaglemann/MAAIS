// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CloudOperations } from "./CloudOperations";
import type { CloudOperationsEvidence } from "./types";

afterEach(cleanup);

const EVIDENCE: CloudOperationsEvidence = {
  candidate: {
    descriptor_hash: "candidate-hash-exact",
    git_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    source_clean: true,
    uv_lock_sha256: "uv-lock-hash",
    dashboard_lock_sha256: "dashboard-lock-hash",
    schema_revision: "0022",
    agent_implementation_hashes: { trend: "agent-hash" },
    dashboard_asset_manifest_sha256: "asset-hash",
    build_definition_sha256: "build-hash",
    status: "qualified",
    creator_deployment_id: "deployment-web-exact",
    registered_at: "2026-08-08T12:00:00Z",
    qualifying_at: "2026-08-08T12:00:01Z",
    qualified_at: "2026-08-08T12:00:02Z",
    qualification_evidence_hash: "qualification-hash",
  },
  run: {
    id: "run-exact",
    experiment_id: "experiment-exact",
    candidate_hash: "candidate-hash-exact",
    manifest_hash: "manifest-hash-exact",
    database_system_identifier: "7669409277984608290",
    railway_environment_id: "environment-exact",
    purpose: "soak",
    status: "active",
    requested_operator_command_id: "command-exact",
    activating_worker_boot_id: "worker-boot-exact",
    continuity_invalidated: false,
    started_at: "2026-08-08T12:00:03Z",
    invalidated_at: null,
    invalidation_reason: null,
    created_at: "2026-08-08T12:00:00Z",
    incidents: [],
  },
  services: {
    items: [
      {
        boot_id: "worker-boot-exact",
        run_id: "run-exact",
        project_id: "project-exact",
        environment_id: "environment-exact",
        service_id: "worker-service-exact",
        deployment_id: "worker-deployment-exact",
        snapshot_id: null,
        replica_id: "worker-replica-exact",
        region: "europe-west4",
        service_role: "worker",
        candidate_hash: "candidate-hash-exact",
        started_at: "2026-08-08T12:00:00Z",
        first_seen_at: "2026-08-08T12:00:01Z",
        last_heartbeat_at: "2026-08-08T12:04:50Z",
        heartbeat_sequence: 5,
        stopped_at: null,
        terminal_reason: null,
      },
      ...["web", "operations"].map((role) => ({
        boot_id: `${role}-boot-exact`,
        run_id: "run-exact",
        project_id: "project-exact",
        environment_id: "environment-exact",
        service_id: `${role}-service-exact`,
        deployment_id: `${role}-deployment-exact`,
        snapshot_id: null,
        replica_id: `${role}-replica-exact`,
        region: "europe-west4",
        service_role: role,
        candidate_hash: "candidate-hash-exact",
        started_at: "2026-08-08T12:00:00Z",
        first_seen_at: "2026-08-08T12:00:01Z",
        last_heartbeat_at: "2026-08-08T12:04:50Z",
        heartbeat_sequence: 5,
        stopped_at: null,
        terminal_reason: null,
      })),
    ],
    limit: 25,
    has_more: false,
    next_before_at: null,
    next_before_id: null,
  },
  health: {
    items: [{
      evaluation_id: "health-exact",
      run_id: "run-exact",
      service_boot_id: "operations-boot-exact",
      overall_status: "healthy",
      failed_check_names: [],
      severity: "info",
      deduplication_key: "health-dedup-exact",
      incident_id: null,
      recovery_of_evaluation_id: null,
      recovered_at: null,
      components: { worm_replication: { status: "ok" } },
      checked_at: "2026-08-08T12:04:30Z",
      content_hash: "health-content-hash",
    }],
    limit: 25,
    has_more: true,
    next_before_at: "2026-08-08T12:04:30Z",
    next_before_id: "health-exact",
  },
  incidents: {
    items: [],
    limit: 25,
    has_more: false,
    next_before_at: null,
    next_before_id: null,
  },
  artifacts: {
    items: [{
      id: "artifact-exact",
      operation_id: "operation-exact",
      publication_attempt_id: "attempt-exact",
      environment: "production",
      candidate_hash: "candidate-hash-exact",
      experiment_id: "experiment-exact",
      run_id: "run-exact",
      artifact_type: "daily_report",
      report_id: "report-exact",
      bundle_content_hash: "bundle-hash-exact",
      size_bytes: 128,
      media_type: "application/json",
      generated_at: "2026-08-08T12:03:00Z",
      recorded_at: "2026-08-08T12:04:00Z",
      producing_deployment_id: "operations-deployment-exact",
      producing_service_id: "operations-service-exact",
      sequence: 1,
      replica_inventory: [{
        store_name: "railway-replica",
        key: "maais/production/report.json",
        etag: "replica-etag",
        version_id: null,
        sha256: "artifact-sha",
        size_bytes: 128,
        content_type: "application/json",
        retention_mode: "COMPLIANCE",
        retain_until: "2026-11-08T00:00:00Z",
        stored_at: "2026-08-08T12:03:30Z",
      }],
      canonical_inventory: [{
        store_name: "canonical-worm",
        key: "maais/production/report.json",
        etag: "canonical-etag",
        version_id: "canonical-version-exact",
        sha256: "artifact-sha",
        size_bytes: 128,
        content_type: "application/json",
        retention_mode: "COMPLIANCE",
        retain_until: "2026-11-08T00:00:00Z",
        stored_at: "2026-08-08T12:03:30Z",
      }],
      previous_evidence_hash: "previous-hash",
      catalog_content_hash: "catalog-hash",
    }],
    limit: 25,
    has_more: false,
    next_before_sequence: null,
  },
  audit: {
    items: [{
      event_id: "audit-event-exact",
      sequence: 12,
      previous_hash: "audit-previous-hash",
      source_role: "operations",
      actor_reference: "service:pseudonymous",
      session_reference: null,
      event_code: "artifact.published",
      reason_code: "dual_store_verified",
      evidence: { artifact_record_id: "artifact-exact" },
      run_id: "run-exact",
      service_boot_id: "operations-boot-exact",
      occurred_at: "2026-08-08T12:04:00Z",
      content_hash: "audit-content-hash",
    }],
    limit: 25,
    has_more: true,
    next_before_sequence: 12,
  },
};

describe("Cloud operations evidence", () => {
  it("shows a quiet compatibility state when an experiment has no cloud registry", () => {
    render(
      <CloudOperations
        evidence={null}
        loading={false}
        error={null}
        fillCount={0}
        rationaleComplete
        onLoadOlder={() => undefined}
      />,
    );

    expect(screen.getByText("No cloud run is registered for this experiment.")).toBeInTheDocument();
  });

  it("shows exact verified identity, continuity, evidence and informational no-fill state", () => {
    const onLoadOlder = vi.fn();
    render(
      <CloudOperations
        evidence={EVIDENCE}
        loading={false}
        error={null}
        fillCount={0}
        rationaleComplete
        now={new Date("2026-08-08T12:05:00Z")}
        onLoadOlder={onLoadOlder}
      />,
    );

    expect(screen.getByRole("heading", { name: "Operations evidence" })).toBeInTheDocument();
    expect(screen.getByText("7669409277984608290")).toBeInTheDocument();
    expect(screen.getByText("worker-deployment-exact")).toBeInTheDocument();
    expect(screen.getByText("worker-replica-exact")).toBeInTheDocument();
    expect(screen.getByText("canonical-version-exact")).toBeInTheDocument();
    expect(screen.getByText("COMPLIANCE")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Required boots continuous")).toBeInTheDocument();
    expect(screen.getByText("Evidence replication verified")).toBeInTheDocument();
    expect(screen.getByText("No fills yet")).toBeInTheDocument();
    expect(screen.getByText("Informational; thresholds stay unchanged.")).toBeInTheDocument();
    expect(screen.getByText("Decision rationale complete")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Trade Ledger" })).toHaveAttribute("href", "#trades");
    expect(screen.getByRole("link", { name: "Decision rationale" })).toHaveAttribute("href", "#ledger");
    expect(screen.getByRole("link", { name: "Research Lab" })).toHaveAttribute("href", "#research");

    fireEvent.click(screen.getByRole("button", { name: "Load older audit evidence" }));
    expect(onLoadOlder).toHaveBeenCalledWith("audit");
  });

  it("makes invalidation, interruption, stale health, replication and rationale failures explicit", () => {
    const failed: CloudOperationsEvidence = {
      ...EVIDENCE,
      run: {
        ...EVIDENCE.run,
        status: "invalidated",
        continuity_invalidated: true,
        invalidated_at: "2026-08-08T12:06:00Z",
        invalidation_reason: "worker_replaced",
      },
      services: {
        ...EVIDENCE.services,
        items: EVIDENCE.services.items.map((service) => service.service_role === "worker"
          ? { ...service, stopped_at: "2026-08-08T12:05:00Z", terminal_reason: "replacement" }
          : service),
      },
      health: {
        ...EVIDENCE.health,
        items: [{
          ...EVIDENCE.health.items[0]!,
          overall_status: "critical",
          severity: "critical",
          failed_check_names: ["worm_replication"],
          checked_at: "2026-08-08T12:00:00Z",
        }],
      },
    };
    render(
      <CloudOperations
        evidence={failed}
        loading={false}
        error={null}
        fillCount={0}
        rationaleComplete={false}
        now={new Date("2026-08-08T12:10:00Z")}
        onLoadOlder={() => undefined}
      />,
    );

    expect(screen.getByText("Continuity invalidated")).toBeInTheDocument();
    expect(screen.getByText("Interrupted")).toBeInTheDocument();
    expect(screen.getByText("Failed evidence replication")).toBeInTheDocument();
    expect(screen.getByText("Stale health")).toBeInTheDocument();
    expect(screen.getByText("Incomplete decision rationale")).toBeInTheDocument();
    expect(screen.getByText("No fills yet")).toBeInTheDocument();
  });

  it("shows standby without treating it as an active interruption", () => {
    render(
      <CloudOperations
        evidence={{
          ...EVIDENCE,
          run: {
            ...EVIDENCE.run,
            status: "standby",
            requested_operator_command_id: null,
            activating_worker_boot_id: null,
            started_at: null,
          },
        }}
        loading={false}
        error={null}
        fillCount={0}
        rationaleComplete
        now={new Date("2026-08-08T12:05:00Z")}
        onLoadOlder={() => undefined}
      />,
    );

    expect(screen.getByText("Standby")).toBeInTheDocument();
    expect(screen.queryByText("Interrupted")).not.toBeInTheDocument();
  });
});
