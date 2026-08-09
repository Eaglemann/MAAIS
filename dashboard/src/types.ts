export type JsonRecord = Record<string, unknown>;

export type AuthMode = "local_token" | "operator_session";

export interface AuthSessionView {
  authenticated: boolean;
  actor: string | null;
  auth_mode: AuthMode;
  expires_at: string | null;
}

export interface LoginResponse {
  authenticated: true;
  actor: string;
  auth_mode: "operator_session";
  csrf_token: string;
  expires_at: string;
}

export interface CsrfTokenResponse {
  csrf_token: string;
}

export interface PaperModelAssumptions {
  model_status: string;
  leverage: number | null;
  maintenance_margin_model: string | null;
  maintenance_margin_rate: string | null;
  liquidation_price_model: string | null;
  exchange_liquidation_parity: boolean | null;
  limitations: string[];
}

export interface ExperimentIdentity {
  id: string;
  name: string;
  mode: string;
  status: string;
  initial_capital: string;
  currency: string;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  failure_reason: string | null;
  git_sha: string;
  worktree_hash: string | null;
  lock_hash: string;
  schema_revision: string;
  config_hash: string;
  manifest_hash: string;
  manifest_schema_version: number;
  model_assumptions: PaperModelAssumptions;
}

export interface AccountOverview {
  source: "manifest_initial_state" | "account_snapshot";
  snapshot_at: string | null;
  account_version: number;
  cash_balance: string;
  equity: string;
  used_margin: string;
  free_margin: string;
  gross_notional: string;
  risk_at_stop: string;
  unrealized_pnl: string;
  realized_pnl: string;
  fees: string;
  funding: string;
  peak_equity: string;
  drawdown: string;
}

export interface RuntimeOverview {
  worker_status: string | null;
  checkpoint_at: string | null;
  checkpoint_version: number | null;
  lease_status: string | null;
  lease_heartbeat_at: string | null;
  lease_expires_at: string | null;
  lease_released_at: string | null;
  lease_epoch: number | null;
  kill_switch_active: boolean;
  kill_switch_reason: string | null;
  control_version: number | null;
}

export interface DecisionCounts {
  total: number;
  completed: number;
  rejected: number;
  quarantined: number;
  neutral: number;
  approved: number;
  directional_rejected: number;
}

export interface OperationalCounts {
  open_positions: number;
  pending_orders: number;
  fills: number;
  open_incidents: number;
  review_incidents: number;
  pending_counterfactuals: number;
}

export interface DataFreshness {
  expected_symbols: number;
  cursor_count: number;
  latest_bar_close_at: string | null;
  latest_cursor_update_at: string | null;
  halted_cursors: number;
  active_recoveries: number;
}

export interface ExperimentListItem {
  experiment: ExperimentIdentity;
  account: AccountOverview;
  runtime: RuntimeOverview;
  decisions: DecisionCounts;
  operations: OperationalCounts;
  freshness: DataFreshness;
}

export interface ExperimentOverview extends ExperimentListItem {
  positions: JsonRecord[];
  pending_orders: JsonRecord[];
  incidents: JsonRecord[];
}

export interface DecisionListItem {
  id: string;
  experiment_id: string;
  market_frame_id: string;
  strategy_version_id: string;
  symbol: string;
  timeframe: string;
  cycle_at: string;
  regime: string;
  status: string;
  direction: string;
  disposition: string;
  reason_code: string;
  quality_status: string;
  consensus_direction: string | null;
  consensus_probability: string | null;
  consensus_confidence: string | null;
  proposal_status: string | null;
  order_status: string | null;
  counterfactual_status: string | null;
  outcome: string;
  created_at: string;
  completed_at: string;
}

export interface DecisionPage {
  items: DecisionListItem[];
  limit: number;
  has_more: boolean;
  next_before_at: string | null;
  next_before_id: string | null;
}

export interface TradeListItem {
  proposal_id: string;
  decision_cycle_id: string;
  proposed_at: string;
  latest_activity_at: string;
  symbol: string;
  direction: string;
  strategy_version_id: string;
  proposal_status: string;
  proposal_reason_code: string;
  approved_notional: string | null;
  decision_disposition: string;
  decision_reason_code: string;
  regime: string;
  official_order_count: number;
  order_statuses: string[];
  fill_count: number;
  filled_quantity: string;
  gross_fill_notional: string;
  fees: string;
  total_slippage: string;
  counterfactual_status: string | null;
  counterfactual_pnl: string | null;
  outcome: string;
}

export interface TradePage {
  items: TradeListItem[];
  limit: number;
  has_more: boolean;
  next_before_at: string | null;
  next_before_id: string | null;
}

export interface CloudCandidateView {
  descriptor_hash: string;
  git_sha: string;
  source_clean: boolean;
  uv_lock_sha256: string;
  dashboard_lock_sha256: string;
  schema_revision: string;
  agent_implementation_hashes: Record<string, string>;
  dashboard_asset_manifest_sha256: string;
  build_definition_sha256: string;
  status: string;
  creator_deployment_id: string;
  registered_at: string;
  qualifying_at: string | null;
  qualified_at: string | null;
  qualification_evidence_hash: string | null;
}

export interface CloudIncidentView {
  id: string;
  experiment_id: string;
  deduplication_key: string;
  severity: string;
  component: string;
  reason_code: string;
  evidence: JsonRecord;
  requires_operator_review: boolean;
  status: string;
  detected_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  acknowledged_by: string | null;
  resolved_by: string | null;
  resolution: string | null;
  changed_at: string;
  version: number;
  content_hash: string;
}

export interface CloudRunView {
  id: string;
  experiment_id: string;
  candidate_hash: string;
  manifest_hash: string;
  database_system_identifier: string;
  railway_environment_id: string;
  purpose: string;
  status: string;
  requested_operator_command_id: string | null;
  activating_worker_boot_id: string | null;
  continuity_invalidated: boolean;
  started_at: string | null;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  created_at: string;
  incidents: CloudIncidentView[];
}

export interface CloudServiceView {
  boot_id: string;
  run_id: string;
  project_id: string;
  environment_id: string;
  service_id: string;
  deployment_id: string;
  snapshot_id: string | null;
  replica_id: string;
  region: string;
  service_role: string;
  candidate_hash: string;
  started_at: string;
  first_seen_at: string;
  last_heartbeat_at: string;
  heartbeat_sequence: number;
  stopped_at: string | null;
  terminal_reason: string | null;
}

export interface CloudServicePage {
  items: CloudServiceView[];
  limit: number;
  has_more: boolean;
  next_before_at: string | null;
  next_before_id: string | null;
}

export interface CloudHealthEvaluationView {
  evaluation_id: string;
  run_id: string;
  service_boot_id: string;
  overall_status: string;
  failed_check_names: string[];
  severity: string;
  deduplication_key: string;
  incident_id: string | null;
  recovery_of_evaluation_id: string | null;
  recovered_at: string | null;
  components: JsonRecord;
  checked_at: string;
  content_hash: string;
}

export interface CloudHealthEvaluationPage {
  items: CloudHealthEvaluationView[];
  limit: number;
  has_more: boolean;
  next_before_at: string | null;
  next_before_id: string | null;
}

export interface CloudIncidentPage {
  items: CloudIncidentView[];
  limit: number;
  has_more: boolean;
  next_before_at: string | null;
  next_before_id: string | null;
}

export interface CloudStoredArtifactView {
  store_name: string;
  key: string;
  etag: string;
  version_id: string | null;
  sha256: string;
  size_bytes: number;
  content_type: string;
  retention_mode: string;
  retain_until: string;
  stored_at: string;
}

export interface CloudArtifactView {
  id: string;
  operation_id: string;
  publication_attempt_id: string;
  environment: string;
  candidate_hash: string;
  experiment_id: string;
  run_id: string;
  artifact_type: string;
  report_id: string;
  bundle_content_hash: string;
  size_bytes: number;
  media_type: string;
  generated_at: string;
  recorded_at: string;
  producing_deployment_id: string;
  producing_service_id: string;
  sequence: number;
  replica_inventory: CloudStoredArtifactView[];
  canonical_inventory: CloudStoredArtifactView[];
  previous_evidence_hash: string;
  catalog_content_hash: string;
}

export interface CloudArtifactPage {
  items: CloudArtifactView[];
  limit: number;
  has_more: boolean;
  next_before_sequence: number | null;
}

export interface CloudAuditEventView {
  event_id: string;
  sequence: number;
  previous_hash: string | null;
  source_role: string;
  actor_reference: string;
  session_reference: string | null;
  event_code: string;
  reason_code: string | null;
  evidence: JsonRecord;
  run_id: string;
  service_boot_id: string | null;
  occurred_at: string;
  content_hash: string;
}

export interface CloudAuditEventPage {
  items: CloudAuditEventView[];
  limit: number;
  has_more: boolean;
  next_before_sequence: number | null;
}

export interface CloudOperationsEvidence {
  candidate: CloudCandidateView;
  run: CloudRunView;
  services: CloudServicePage;
  health: CloudHealthEvaluationPage;
  incidents: CloudIncidentPage;
  artifacts: CloudArtifactPage;
  audit: CloudAuditEventPage;
}

export type CloudEvidencePageKind = "services" | "health" | "incidents" | "artifacts" | "audit";

export interface SequenceCursor {
  beforeSequence: number;
}

export type OperatorCommandType =
  | "start"
  | "pause"
  | "resume"
  | "stop"
  | "emergency_halt"
  | "flatten"
  | "acknowledge_incident"
  | "resolve_incident"
  | "reset_kill_switch";

export type OperatorCommandStatus = "requested" | "accepted" | "completed" | "rejected";

export interface OperatorCommand {
  command_id: string;
  experiment_id: string;
  command_type: OperatorCommandType;
  status: OperatorCommandStatus;
  idempotency_key: string;
  actor: string;
  reason: string;
  payload: JsonRecord;
  operator_confirmed: boolean;
  request_hash: string;
  requested_at: string;
  version: number;
  accepted_at: string | null;
  accepted_by: string | null;
  completed_at: string | null;
  result: JsonRecord | null;
}

export interface OperatorCommandPage {
  items: OperatorCommand[];
  limit: number;
}

export interface OperatorActionDraft {
  commandType: OperatorCommandType;
  reason: string;
  payload: JsonRecord;
  confirmation: string;
}

export interface ResearchCounterfactual {
  id: string;
  proposal_id: string;
  decision_cycle_id: string;
  symbol: string;
  direction: string;
  rejection_gate: string;
  status: string;
  maximum_favorable_excursion: string;
  maximum_adverse_excursion: string;
  outcome_15m: string | null;
  outcome_1h: string | null;
  outcome_4h: string | null;
  outcome_24h: string | null;
  no_fill_reason: string | null;
  hypothetical_exit_reason: string | null;
  hypothetical_pnl: string | null;
  created_at: string;
  closed_at: string | null;
  content_hash: string;
}

export interface ResearchExecutionSensitivity {
  id: string;
  order_intent_id: string;
  proposal_id: string;
  decision_cycle_id: string;
  symbol: string;
  scenario: string;
  calculated_at: string;
  outcome: JsonRecord;
}

export interface ResearchLabView {
  official_account_inclusion: "excluded";
  analytics_as_of: string | null;
  equity_curve: Array<{ at: string; equity: string; drawdown: string }>;
  cost_waterfall: {
    initial_capital: string;
    gross_realized_pnl: string;
    fees: string;
    funding: string;
    unrealized_pnl: string;
    net_change: string;
    ending_equity: string;
    reconciles: boolean;
  };
  performance: {
    basis: string;
    closed_trade_allocations: number;
    wins: number;
    losses: number;
    breakeven: number;
    win_rate: string | null;
    average_win: string | null;
    average_loss: string | null;
    expectancy: string | null;
    profit_factor: string | null;
    average_r_multiple: string | null;
    maximum_favorable_excursion: string | null;
    maximum_adverse_excursion: string | null;
  };
  attribution: Record<string, Array<{
    key: string;
    trades: number;
    wins: number;
    losses: number;
    win_rate: string;
    net_pnl_ex_funding: string;
    expectancy: string;
  }>>;
  calibration: Record<string, {
    sample_size: number;
    brier_score: string | null;
    mean_probability: string | null;
    observed_win_rate: string | null;
  }>;
  gate_value: {
    interpretation: string;
    resolved_sample_size: number;
    by_gate: Array<{
      gate: string;
      sample_size: number;
      hypothetical_pnl: string;
      avoided_pnl: string;
    }>;
  };
  cost_sensitivity: Record<string, {
    sample_size: number;
    execution_cost: string;
    marked_pnl: string;
  }>;
  benchmarks: {
    buy_and_hold: JsonRecord;
    flat_cash: JsonRecord;
  };
  availability: Record<string, {
    status: string;
    reason: string | null;
    sample_size: number;
  }>;
  counterfactuals: ResearchCounterfactual[];
  execution_sensitivities: ResearchExecutionSensitivity[];
  limit_per_kind: number;
}

export interface OutboxCursorEvent {
  cursor: number;
  event_type: string;
  created_at: string;
  payload: JsonRecord;
}

export interface OutboxCursorPage {
  items: OutboxCursorEvent[];
  limit: number;
  has_more: boolean;
  next_cursor: number;
}

export type EventFeedStatus = "catching_up" | "live" | "reconnecting" | "stopped";

export interface AuditEvent {
  id: string;
  global_position: number;
  aggregate_id: string;
  aggregate_type: string;
  stream_version: number;
  event_type: string;
  event_version: number;
  payload: JsonRecord;
  metadata: JsonRecord;
  occurred_at: string;
  recorded_at: string;
}

export interface DecisionDetail {
  decision: DecisionListItem;
  cycle: JsonRecord;
  market_frame: JsonRecord;
  quality_evaluations: JsonRecord[];
  agents: JsonRecord[];
  summary: JsonRecord | null;
  gates: JsonRecord[];
  proposal: JsonRecord | null;
  orders: JsonRecord[];
  counterfactual: JsonRecord | null;
  incident: JsonRecord | null;
  timeline: AuditEvent[];
  lineage_hashes: Record<string, string>;
}

export interface DecisionFilters {
  symbol: string;
  status: string;
  direction: string;
  disposition: string;
  reasonCode: string;
  fromAt: string;
  toAt: string;
  regime: string;
  strategyVersionId: string;
  gateType: string;
  gatePassed: "" | "true" | "false";
  agentName: string;
  agentDirection: string;
  proposalStatus: string;
  orderStatus: string;
  outcome: string;
}

export interface TradeFilters {
  symbol: string;
  fromAt: string;
  toAt: string;
  direction: string;
  regime: string;
  strategyVersionId: string;
  proposalStatus: string;
  decisionDisposition: string;
  orderStatus: string;
  counterfactualStatus: string;
  outcome: string;
}

export interface PageCursor {
  beforeAt: string;
  beforeId: string;
}
