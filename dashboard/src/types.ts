export type JsonRecord = Record<string, unknown>;

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
}

export interface TradePage {
  items: TradeListItem[];
  limit: number;
  has_more: boolean;
  next_before_at: string | null;
  next_before_id: string | null;
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
  disposition: string;
  reasonCode: string;
}
