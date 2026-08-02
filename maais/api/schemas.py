from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReadModel(BaseModel):
    model_config = ConfigDict(frozen=True)


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
    created_at: datetime
    completed_at: datetime


class DecisionPage(ReadModel):
    items: tuple[DecisionListItem, ...]
    limit: int
    has_more: bool
    next_before: datetime | None


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
