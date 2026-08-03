from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from maais.operations.operator_commands import CommandStatus, CommandType


class ReadModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ApiHealth(ReadModel):
    service: str
    status: str
    database_transaction: str
    schema_revision: str
    checked_at: datetime


class PaperModelAssumptions(ReadModel):
    model_status: str
    leverage: int | None
    maintenance_margin_model: str | None
    maintenance_margin_rate: Decimal | None
    liquidation_price_model: str | None
    exchange_liquidation_parity: bool | None
    limitations: tuple[str, ...]


class ExperimentIdentity(ReadModel):
    id: UUID
    name: str
    mode: str
    status: str
    initial_capital: Decimal
    currency: str
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    failure_reason: str | None
    git_sha: str
    worktree_hash: str | None
    lock_hash: str
    schema_revision: str
    config_hash: str
    manifest_hash: str
    manifest_schema_version: int
    model_assumptions: PaperModelAssumptions


class AccountOverview(ReadModel):
    source: str
    snapshot_at: datetime | None
    account_version: int
    cash_balance: Decimal
    equity: Decimal
    used_margin: Decimal
    free_margin: Decimal
    gross_notional: Decimal
    risk_at_stop: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    peak_equity: Decimal
    drawdown: Decimal


class RuntimeOverview(ReadModel):
    worker_status: str | None
    checkpoint_at: datetime | None
    checkpoint_version: int | None
    lease_status: str | None
    lease_heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    lease_released_at: datetime | None
    lease_epoch: int | None
    kill_switch_active: bool
    kill_switch_reason: str | None
    control_version: int | None


class DecisionCounts(ReadModel):
    total: int = 0
    completed: int = 0
    rejected: int = 0
    quarantined: int = 0
    neutral: int = 0
    approved: int = 0
    directional_rejected: int = 0


class OperationalCounts(ReadModel):
    open_positions: int = 0
    pending_orders: int = 0
    fills: int = 0
    open_incidents: int = 0
    review_incidents: int = 0
    pending_counterfactuals: int = 0


class DataFreshness(ReadModel):
    expected_symbols: int
    cursor_count: int
    latest_bar_close_at: datetime | None
    latest_cursor_update_at: datetime | None
    halted_cursors: int
    active_recoveries: int


class ExperimentListItem(ReadModel):
    experiment: ExperimentIdentity
    account: AccountOverview
    runtime: RuntimeOverview
    decisions: DecisionCounts
    operations: OperationalCounts
    freshness: DataFreshness


class ExperimentOverview(ExperimentListItem):
    positions: tuple[dict[str, object], ...] = ()
    pending_orders: tuple[dict[str, object], ...] = ()
    incidents: tuple[dict[str, object], ...] = ()


class DecisionListItem(ReadModel):
    id: UUID
    experiment_id: UUID
    market_frame_id: UUID
    strategy_version_id: UUID
    symbol: str
    timeframe: str
    cycle_at: datetime
    regime: str
    status: str
    direction: str
    disposition: str
    reason_code: str
    quality_status: str
    consensus_direction: str | None
    consensus_probability: Decimal | None
    consensus_confidence: Decimal | None
    proposal_status: str | None
    order_status: str | None
    counterfactual_status: str | None
    outcome: str
    created_at: datetime
    completed_at: datetime


class DecisionPage(ReadModel):
    items: tuple[DecisionListItem, ...]
    limit: int
    has_more: bool
    next_before_at: datetime | None
    next_before_id: UUID | None


class TradeListItem(ReadModel):
    proposal_id: UUID
    decision_cycle_id: UUID
    proposed_at: datetime
    latest_activity_at: datetime
    symbol: str
    direction: str
    strategy_version_id: UUID
    proposal_status: str
    proposal_reason_code: str
    approved_notional: Decimal | None
    decision_disposition: str
    decision_reason_code: str
    regime: str
    official_order_count: int
    order_statuses: tuple[str, ...]
    fill_count: int
    filled_quantity: Decimal
    gross_fill_notional: Decimal
    fees: Decimal
    total_slippage: Decimal
    counterfactual_status: str | None
    counterfactual_pnl: Decimal | None
    outcome: str


class TradePage(ReadModel):
    items: tuple[TradeListItem, ...]
    limit: int
    has_more: bool
    next_before_at: datetime | None
    next_before_id: UUID | None


class ResearchCounterfactual(ReadModel):
    id: UUID
    proposal_id: UUID
    decision_cycle_id: UUID
    symbol: str
    direction: str
    rejection_gate: str
    status: str
    maximum_favorable_excursion: Decimal
    maximum_adverse_excursion: Decimal
    outcome_15m: Decimal | None
    outcome_1h: Decimal | None
    outcome_4h: Decimal | None
    outcome_24h: Decimal | None
    no_fill_reason: str | None
    hypothetical_exit_reason: str | None
    hypothetical_pnl: Decimal | None
    created_at: datetime
    closed_at: datetime | None
    content_hash: str


class ResearchExecutionSensitivity(ReadModel):
    id: UUID
    order_intent_id: UUID
    proposal_id: UUID
    decision_cycle_id: UUID
    symbol: str
    scenario: str
    calculated_at: datetime
    outcome: Mapping[str, object]


class ResearchLabView(ReadModel):
    official_account_inclusion: str = "excluded"
    counterfactuals: tuple[ResearchCounterfactual, ...]
    execution_sensitivities: tuple[ResearchExecutionSensitivity, ...]
    limit_per_kind: int


class AuditEvent(ReadModel):
    id: UUID
    global_position: int
    aggregate_id: UUID
    aggregate_type: str
    stream_version: int
    event_type: str
    event_version: int
    payload: Mapping[str, object]
    metadata: Mapping[str, object]
    occurred_at: datetime
    recorded_at: datetime


class DecisionDetail(ReadModel):
    decision: DecisionListItem
    cycle: dict[str, object]
    market_frame: dict[str, object]
    quality_evaluations: tuple[dict[str, object], ...]
    agents: tuple[dict[str, object], ...]
    summary: dict[str, object] | None
    gates: tuple[dict[str, object], ...]
    proposal: dict[str, object] | None
    orders: tuple[dict[str, object], ...]
    counterfactual: dict[str, object] | None
    incident: dict[str, object] | None
    timeline: tuple[AuditEvent, ...]
    lineage_hashes: dict[str, str] = Field(default_factory=dict)


class OperatorCommandRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command_type: CommandType
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)
    payload: dict[str, object] = Field(default_factory=dict)
    confirmation: str | None = Field(default=None, max_length=128)


class OperatorCommandView(ReadModel):
    command_id: UUID
    experiment_id: UUID
    command_type: CommandType
    status: CommandStatus
    idempotency_key: str
    actor: str
    reason: str
    payload: Mapping[str, object]
    operator_confirmed: bool
    request_hash: str
    requested_at: datetime
    version: int
    accepted_at: datetime | None
    accepted_by: str | None
    completed_at: datetime | None
    result: Mapping[str, object] | None


class OperatorCommandPage(ReadModel):
    items: tuple[OperatorCommandView, ...]
    limit: int


class OutboxCursorEvent(ReadModel):
    cursor: int
    event_type: str
    created_at: datetime
    payload: Mapping[str, object]


class OutboxCursorPage(ReadModel):
    items: tuple[OutboxCursorEvent, ...]
    limit: int
    has_more: bool
    next_cursor: int
