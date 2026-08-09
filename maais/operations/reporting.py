"""Deterministic daily paper-trading reports and immutable local artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from maais.analytics.query import load_research_dataset
from maais.config.paper_candidate import OFFICIAL_MARGIN_POLICY, OFFICIAL_MODEL_LIMITATIONS
from maais.config.settings import get_settings
from maais.db.models.accounts import AccountSnapshotModel, FundingEntryModel, PositionModel
from maais.db.models.counterfactuals import CounterfactualModel
from maais.db.models.decisions import (
    AgentEvaluationModel,
    DecisionCycleModel,
    GateEvaluationModel,
    MarketFrameModel,
    TradeProposalModel,
)
from maais.db.models.execution import FillModel, OrderEventModel, OrderIntentModel
from maais.db.models.experiments import AgentVersionModel, ExperimentModel
from maais.db.models.ledger import DomainEventModel
from maais.db.models.operations import (
    DataQualityEvaluationModel,
    IncidentModel,
    MarketCursorModel,
    MarketRecoveryRunModel,
    OperatorCommandModel,
    TradingControlModel,
    WorkerCheckpointModel,
    WorkerLeaseModel,
)
from maais.db.replay import verify_ledger_consistency
from maais.domain.json import content_hash, to_json_data
from maais.operations.verification import establish_read_only_snapshot

BERLIN = ZoneInfo("Europe/Berlin")
UTC = timezone.utc
ZERO = Decimal("0")
REPORT_SCHEMA_VERSION = 3
_PENDING_ORDER_STATUSES = ("created", "authorized", "accepted", "partially_filled")
_OPEN_COUNTERFACTUAL_STATUSES = ("pending", "open")


def _report_model_assumptions(manifest: Mapping[str, object]) -> dict[str, object]:
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("experiment manifest configuration must be an object")
    risk = configuration.get("risk")
    if not isinstance(risk, Mapping):
        raise ValueError("experiment manifest risk policy must be an object")
    margin = {"leverage": risk.get("leverage"), **dict(OFFICIAL_MARGIN_POLICY)}
    if any(risk.get(name) != value for name, value in margin.items()):
        raise ValueError("experiment manifest margin policy differs from the reporting model")
    return {
        "margin": margin,
        "limitations": list(OFFICIAL_MODEL_LIMITATIONS),
    }


@dataclass(frozen=True, slots=True)
class DailyWindow:
    report_date: date
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True, slots=True)
class ReportBundlePaths:
    directory: Path
    json_path: Path
    markdown_path: Path
    decisions_csv_path: Path
    decisions_parquet_path: Path
    execution_csv_path: Path
    execution_parquet_path: Path
    manifest_path: Path


def berlin_daily_window(report_date: date) -> DailyWindow:
    start_local = datetime.combine(report_date, time.min, BERLIN)
    end_local = datetime.combine(report_date + timedelta(days=1), time.min, BERLIN)
    return DailyWindow(
        report_date=report_date,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(UTC),
        end_utc=end_local.astimezone(UTC),
    )


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _sum(values: list[Decimal]) -> Decimal:
    return sum(values, start=ZERO)


def _account_values(
    experiment: ExperimentModel,
    snapshot: AccountSnapshotModel | None,
) -> dict[str, object]:
    if snapshot is None:
        return {
            "source": "manifest_initial_state",
            "snapshot_at": None,
            "account_version": 0,
            "cash_balance": experiment.initial_capital,
            "equity": experiment.initial_capital,
            "used_margin": ZERO,
            "free_margin": experiment.initial_capital,
            "gross_notional": ZERO,
            "risk_at_stop": ZERO,
            "unrealized_pnl": ZERO,
            "realized_pnl": ZERO,
            "fees": ZERO,
            "funding": ZERO,
            "peak_equity": experiment.initial_capital,
            "drawdown": ZERO,
        }
    return {
        "source": "account_snapshot",
        "snapshot_at": snapshot.snapshot_at,
        "account_version": snapshot.account_version,
        "cash_balance": snapshot.cash_balance,
        "equity": snapshot.equity,
        "used_margin": snapshot.used_margin,
        "free_margin": snapshot.free_margin,
        "gross_notional": snapshot.gross_notional,
        "risk_at_stop": snapshot.risk_at_stop,
        "unrealized_pnl": snapshot.unrealized_pnl,
        "realized_pnl": snapshot.realized_pnl,
        "fees": snapshot.fees,
        "funding": snapshot.funding,
        "peak_equity": snapshot.peak_equity,
        "drawdown": snapshot.drawdown,
    }


async def _snapshot_at_or_before(
    session: AsyncSession,
    experiment_id: UUID,
    cutoff: datetime,
) -> AccountSnapshotModel | None:
    return await session.scalar(
        select(AccountSnapshotModel)
        .where(
            AccountSnapshotModel.experiment_id == experiment_id,
            AccountSnapshotModel.snapshot_at <= cutoff,
        )
        .order_by(
            AccountSnapshotModel.snapshot_at.desc(), AccountSnapshotModel.account_version.desc()
        )
        .limit(1)
    )


def _row_evidence(kind: str, row_id: object, values: dict[str, object]) -> dict[str, object]:
    return {"kind": kind, "id": str(row_id), "hash": content_hash(values)}


async def build_daily_report(
    session: AsyncSession,
    experiment_id: UUID,
    report_date: date,
    *,
    generated_at: datetime,
) -> dict[str, object]:
    """Build one report from authoritative rows without mutating database state."""
    if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(0):
        raise ValueError("generated_at must be UTC-aware")
    experiment = await session.get(ExperimentModel, experiment_id)
    if experiment is None:
        raise LookupError(f"experiment not found: {experiment_id}")
    if experiment.mode != "paper_live":
        raise ValueError("daily operational reports require a paper_live experiment")

    window = berlin_daily_window(report_date)
    cutoff = min(window.end_utc, generated_at)
    if cutoff <= window.start_utc:
        raise ValueError("generated_at must be after the report day begins")

    decision_rows = (
        await session.execute(
            select(
                DecisionCycleModel.id,
                DecisionCycleModel.market_frame_id,
                DecisionCycleModel.cycle_at,
                DecisionCycleModel.symbol,
                DecisionCycleModel.timeframe,
                DecisionCycleModel.status,
                DecisionCycleModel.direction,
                DecisionCycleModel.disposition,
                DecisionCycleModel.reason_code,
                DecisionCycleModel.regime,
                DecisionCycleModel.content_hash,
            )
            .where(
                DecisionCycleModel.experiment_id == experiment_id,
                DecisionCycleModel.cycle_at >= window.start_utc,
                DecisionCycleModel.cycle_at < cutoff,
            )
            .order_by(DecisionCycleModel.cycle_at, DecisionCycleModel.id)
        )
    ).all()

    agent_rows = (
        await session.execute(
            select(
                AgentEvaluationModel.id,
                AgentVersionModel.agent_name,
                AgentVersionModel.maturity,
                AgentEvaluationModel.direction,
                AgentEvaluationModel.compatible,
                AgentEvaluationModel.enabled,
                AgentEvaluationModel.reason_codes_json,
            )
            .join(
                DecisionCycleModel, DecisionCycleModel.id == AgentEvaluationModel.decision_cycle_id
            )
            .join(AgentVersionModel, AgentVersionModel.id == AgentEvaluationModel.agent_version_id)
            .where(
                DecisionCycleModel.experiment_id == experiment_id,
                DecisionCycleModel.cycle_at >= window.start_utc,
                DecisionCycleModel.cycle_at < cutoff,
            )
            .order_by(AgentEvaluationModel.id)
        )
    ).all()
    gate_rows = (
        await session.execute(
            select(
                GateEvaluationModel.id,
                GateEvaluationModel.gate_type,
                GateEvaluationModel.passed,
                GateEvaluationModel.reason_code,
            )
            .join(
                DecisionCycleModel, DecisionCycleModel.id == GateEvaluationModel.decision_cycle_id
            )
            .where(
                DecisionCycleModel.experiment_id == experiment_id,
                DecisionCycleModel.cycle_at >= window.start_utc,
                DecisionCycleModel.cycle_at < cutoff,
            )
            .order_by(GateEvaluationModel.id)
        )
    ).all()
    quality_rows = (
        await session.execute(
            select(
                DataQualityEvaluationModel.id,
                DataQualityEvaluationModel.check_name,
                DataQualityEvaluationModel.required,
                DataQualityEvaluationModel.status,
                DataQualityEvaluationModel.reason_code,
                DataQualityEvaluationModel.content_hash,
            )
            .join(
                DecisionCycleModel,
                DecisionCycleModel.market_frame_id == DataQualityEvaluationModel.market_frame_id,
            )
            .where(
                DecisionCycleModel.experiment_id == experiment_id,
                DecisionCycleModel.cycle_at >= window.start_utc,
                DecisionCycleModel.cycle_at < cutoff,
            )
            .order_by(DataQualityEvaluationModel.id)
        )
    ).all()
    proposal_rows = (
        await session.execute(
            select(
                TradeProposalModel.id,
                TradeProposalModel.decision_cycle_id,
                TradeProposalModel.symbol,
                TradeProposalModel.status,
                TradeProposalModel.direction,
                TradeProposalModel.reason_code,
                TradeProposalModel.proposed_at,
                TradeProposalModel.approved_notional,
            )
            .where(
                TradeProposalModel.experiment_id == experiment_id,
                TradeProposalModel.proposed_at >= window.start_utc,
                TradeProposalModel.proposed_at < cutoff,
            )
            .order_by(TradeProposalModel.id)
        )
    ).all()
    order_rows = (
        await session.execute(
            select(
                OrderIntentModel.id,
                OrderIntentModel.proposal_id,
                OrderIntentModel.symbol,
                OrderIntentModel.status,
                OrderIntentModel.side,
                OrderIntentModel.position_effect,
                OrderIntentModel.quantity,
                OrderIntentModel.filled_quantity,
                OrderIntentModel.created_at,
                OrderIntentModel.content_hash,
            )
            .where(
                OrderIntentModel.experiment_id == experiment_id,
                OrderIntentModel.created_at >= window.start_utc,
                OrderIntentModel.created_at < cutoff,
            )
            .order_by(OrderIntentModel.id)
        )
    ).all()
    order_event_rows = (
        await session.execute(
            select(
                OrderEventModel.id,
                OrderEventModel.order_intent_id,
                OrderEventModel.event_type,
                OrderEventModel.event_at,
                OrderEventModel.payload_json,
            )
            .join(OrderIntentModel, OrderIntentModel.id == OrderEventModel.order_intent_id)
            .where(
                OrderIntentModel.experiment_id == experiment_id,
                OrderEventModel.event_at >= window.start_utc,
                OrderEventModel.event_at < cutoff,
            )
            .order_by(OrderEventModel.id)
        )
    ).all()
    fill_rows = (
        await session.execute(
            select(
                FillModel.id,
                FillModel.order_intent_id,
                FillModel.fill_at,
                FillModel.quantity,
                FillModel.price,
                FillModel.fee,
                FillModel.spread_cost,
                FillModel.depth_slippage,
                FillModel.latency_slippage,
                FillModel.total_slippage,
            )
            .join(OrderIntentModel, OrderIntentModel.id == FillModel.order_intent_id)
            .where(
                OrderIntentModel.experiment_id == experiment_id,
                FillModel.fill_at >= window.start_utc,
                FillModel.fill_at < cutoff,
            )
            .order_by(FillModel.id)
        )
    ).all()
    funding_rows = (
        await session.execute(
            select(
                FundingEntryModel.id,
                FundingEntryModel.funding_at,
                FundingEntryModel.amount,
                FundingEntryModel.rate,
                FundingEntryModel.notional,
            )
            .where(
                FundingEntryModel.experiment_id == experiment_id,
                FundingEntryModel.funding_at >= window.start_utc,
                FundingEntryModel.funding_at < cutoff,
            )
            .order_by(FundingEntryModel.id)
        )
    ).all()
    incident_rows = (
        await session.execute(
            select(
                IncidentModel.id,
                IncidentModel.severity,
                IncidentModel.component,
                IncidentModel.reason_code,
                IncidentModel.requires_operator_review,
                IncidentModel.status,
                IncidentModel.content_hash,
            )
            .where(
                IncidentModel.experiment_id == experiment_id,
                IncidentModel.detected_at >= window.start_utc,
                IncidentModel.detected_at < cutoff,
            )
            .order_by(IncidentModel.id)
        )
    ).all()
    recovery_rows = (
        await session.execute(
            select(
                MarketRecoveryRunModel.id,
                MarketRecoveryRunModel.status,
                MarketRecoveryRunModel.symbol,
                MarketRecoveryRunModel.failure_reason,
                MarketRecoveryRunModel.content_hash,
            )
            .where(
                MarketRecoveryRunModel.experiment_id == experiment_id,
                MarketRecoveryRunModel.started_at >= window.start_utc,
                MarketRecoveryRunModel.started_at < cutoff,
            )
            .order_by(MarketRecoveryRunModel.id)
        )
    ).all()
    counterfactual_rows = (
        await session.execute(
            select(
                CounterfactualModel.id,
                CounterfactualModel.status,
                CounterfactualModel.rejection_gate,
                CounterfactualModel.hypothetical_pnl,
                CounterfactualModel.content_hash,
            )
            .where(
                CounterfactualModel.experiment_id == experiment_id,
                CounterfactualModel.created_at >= window.start_utc,
                CounterfactualModel.created_at < cutoff,
            )
            .order_by(CounterfactualModel.id)
        )
    ).all()
    restart_rows = (
        await session.execute(
            select(DomainEventModel.id, DomainEventModel.stream_version)
            .where(
                DomainEventModel.aggregate_type == "worker_checkpoint",
                DomainEventModel.aggregate_id == experiment_id,
                DomainEventModel.event_type == "worker_checkpoint.starting",
                DomainEventModel.occurred_at >= window.start_utc,
                DomainEventModel.occurred_at < cutoff,
            )
            .order_by(DomainEventModel.global_position)
        )
    ).all()
    operator_action_rows = (
        await session.execute(
            select(
                DomainEventModel.id,
                DomainEventModel.global_position,
                DomainEventModel.aggregate_id,
                DomainEventModel.stream_version,
                DomainEventModel.event_type,
                DomainEventModel.event_version,
                DomainEventModel.payload_json,
                DomainEventModel.metadata_json,
                DomainEventModel.occurred_at,
                DomainEventModel.recorded_at,
            )
            .join(
                OperatorCommandModel,
                OperatorCommandModel.id == DomainEventModel.aggregate_id,
            )
            .where(
                DomainEventModel.aggregate_type == "operator_command",
                OperatorCommandModel.experiment_id == experiment_id,
                DomainEventModel.occurred_at >= window.start_utc,
                DomainEventModel.occurred_at < cutoff,
            )
            .order_by(DomainEventModel.global_position)
        )
    ).all()

    start_snapshot = await _snapshot_at_or_before(session, experiment_id, window.start_utc)
    end_snapshot = await _snapshot_at_or_before(session, experiment_id, cutoff)
    period_snapshots = (
        await session.scalars(
            select(AccountSnapshotModel)
            .where(
                AccountSnapshotModel.experiment_id == experiment_id,
                AccountSnapshotModel.snapshot_at >= window.start_utc,
                AccountSnapshotModel.snapshot_at < cutoff,
            )
            .order_by(AccountSnapshotModel.snapshot_at, AccountSnapshotModel.account_version)
        )
    ).all()
    start_account = _account_values(experiment, start_snapshot)
    end_account = _account_values(experiment, end_snapshot)
    max_drawdown = max(
        (snapshot.drawdown for snapshot in period_snapshots),
        default=cast(Decimal, end_account["drawdown"]),
    )
    peak_exposure = max(
        (snapshot.gross_notional for snapshot in period_snapshots),
        default=cast(Decimal, end_account["gross_notional"]),
    )
    peak_risk_at_stop = max(
        (snapshot.risk_at_stop for snapshot in period_snapshots),
        default=cast(Decimal, end_account["risk_at_stop"]),
    )
    peak_margin = max(
        (snapshot.used_margin for snapshot in period_snapshots),
        default=cast(Decimal, end_account["used_margin"]),
    )

    checkpoint = await session.get(WorkerCheckpointModel, experiment_id)
    lease = await session.get(WorkerLeaseModel, experiment_id)
    control = await session.get(TradingControlModel, experiment_id)
    open_position_rows = (
        await session.execute(
            select(
                PositionModel.id,
                PositionModel.symbol,
                PositionModel.quantity,
                PositionModel.mark_price,
                PositionModel.version,
            ).where(
                PositionModel.experiment_id == experiment_id,
                PositionModel.status == "open",
            )
        )
    ).all()
    pending_order_rows = (
        await session.execute(
            select(
                OrderIntentModel.id,
                OrderIntentModel.status,
                OrderIntentModel.version,
                OrderIntentModel.content_hash,
            )
            .where(
                OrderIntentModel.experiment_id == experiment_id,
                OrderIntentModel.status.in_(_PENDING_ORDER_STATUSES),
            )
            .order_by(OrderIntentModel.id)
        )
    ).all()
    unresolved_counterfactual_rows = (
        await session.execute(
            select(
                CounterfactualModel.id,
                CounterfactualModel.status,
                CounterfactualModel.version,
                CounterfactualModel.content_hash,
            )
            .where(
                CounterfactualModel.experiment_id == experiment_id,
                CounterfactualModel.status.in_(_OPEN_COUNTERFACTUAL_STATUSES),
            )
            .order_by(CounterfactualModel.id)
        )
    ).all()
    operator_review_rows = (
        await session.execute(
            select(IncidentModel.id, IncidentModel.content_hash)
            .where(
                IncidentModel.experiment_id == experiment_id,
                IncidentModel.status != "resolved",
                IncidentModel.requires_operator_review.is_(True),
            )
            .order_by(IncidentModel.id)
        )
    ).all()
    cursor_rows = (
        await session.execute(
            select(
                MarketCursorModel.id,
                MarketCursorModel.status,
                MarketCursorModel.bar_close_at,
                MarketCursorModel.updated_at,
                MarketCursorModel.content_hash,
            )
            .where(MarketCursorModel.experiment_id == experiment_id)
            .order_by(MarketCursorModel.id)
        )
    ).all()
    latest_bar_close_at = max((row.bar_close_at for row in cursor_rows), default=None)
    latest_cursor_update_at = max((row.updated_at for row in cursor_rows), default=None)
    manifest_symbols = experiment.manifest_json.get("symbols", [])
    expected_symbols = len(manifest_symbols) if isinstance(manifest_symbols, list) else 0

    ledger = await verify_ledger_consistency(session)
    evidence: list[dict[str, object]] = []
    for row in decision_rows:
        evidence.append({"kind": "decision", "id": str(row.id), "hash": row.content_hash})
    frame_ids = sorted({row.market_frame_id for row in decision_rows}, key=str)
    if frame_ids:
        frames = (
            await session.execute(
                select(MarketFrameModel.id, MarketFrameModel.content_hash)
                .where(MarketFrameModel.id.in_(frame_ids))
                .order_by(MarketFrameModel.id)
            )
        ).all()
        evidence.extend(
            {"kind": "market_frame", "id": str(row.id), "hash": row.content_hash} for row in frames
        )
    evidence.extend(
        {"kind": "data_quality", "id": str(row.id), "hash": row.content_hash}
        for row in quality_rows
    )
    evidence.extend(
        _row_evidence(
            "agent_evaluation",
            row.id,
            {
                "agent_name": row.agent_name,
                "maturity": row.maturity,
                "direction": row.direction,
                "compatible": row.compatible,
                "enabled": row.enabled,
                "reason_codes": row.reason_codes_json,
            },
        )
        for row in agent_rows
    )
    evidence.extend(
        _row_evidence(
            "gate_evaluation",
            row.id,
            {"gate_type": row.gate_type, "passed": row.passed, "reason_code": row.reason_code},
        )
        for row in gate_rows
    )
    evidence.extend(
        _row_evidence(
            "proposal",
            row.id,
            {
                "status": row.status,
                "direction": row.direction,
                "reason_code": row.reason_code,
                "approved_notional": row.approved_notional,
            },
        )
        for row in proposal_rows
    )
    evidence.extend(
        {"kind": "order", "id": str(row.id), "hash": row.content_hash} for row in order_rows
    )
    evidence.extend(
        _row_evidence(
            "order_event",
            row.id,
            {"event_type": row.event_type, "payload": row.payload_json},
        )
        for row in order_event_rows
    )
    evidence.extend(
        _row_evidence(
            "fill",
            row.id,
            {
                "quantity": row.quantity,
                "price": row.price,
                "fee": row.fee,
                "spread_cost": row.spread_cost,
                "depth_slippage": row.depth_slippage,
                "latency_slippage": row.latency_slippage,
                "total_slippage": row.total_slippage,
            },
        )
        for row in fill_rows
    )
    evidence.extend(
        _row_evidence(
            "funding",
            row.id,
            {"amount": row.amount, "rate": row.rate, "notional": row.notional},
        )
        for row in funding_rows
    )
    evidence.extend(
        {"kind": "incident", "id": str(row.id), "hash": row.content_hash} for row in incident_rows
    )
    evidence.extend(
        {"kind": "market_recovery", "id": str(row.id), "hash": row.content_hash}
        for row in recovery_rows
    )
    evidence.extend(
        {"kind": "counterfactual", "id": str(row.id), "hash": row.content_hash}
        for row in counterfactual_rows
    )
    unique_snapshots = {
        snapshot.id: snapshot
        for snapshot in (start_snapshot, end_snapshot, *period_snapshots)
        if snapshot is not None
    }
    evidence.extend(
        _row_evidence(
            "account_snapshot",
            snapshot.id,
            {
                "account_version": snapshot.account_version,
                "snapshot_at": snapshot.snapshot_at,
                "cash_balance": snapshot.cash_balance,
                "equity": snapshot.equity,
                "used_margin": snapshot.used_margin,
                "free_margin": snapshot.free_margin,
                "gross_notional": snapshot.gross_notional,
                "risk_at_stop": snapshot.risk_at_stop,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "realized_pnl": snapshot.realized_pnl,
                "fees": snapshot.fees,
                "funding": snapshot.funding,
                "peak_equity": snapshot.peak_equity,
                "drawdown": snapshot.drawdown,
            },
        )
        for snapshot in unique_snapshots.values()
    )
    evidence.extend(
        _row_evidence(
            "worker_start",
            row.id,
            {"stream_version": row.stream_version},
        )
        for row in restart_rows
    )
    evidence.extend(
        _row_evidence(
            "operator_action",
            row.id,
            {
                "global_position": row.global_position,
                "command_id": row.aggregate_id,
                "stream_version": row.stream_version,
                "event_type": row.event_type,
                "event_version": row.event_version,
                "payload": row.payload_json,
                "metadata": row.metadata_json,
                "occurred_at": row.occurred_at,
                "recorded_at": row.recorded_at,
            },
        )
        for row in operator_action_rows
    )
    if checkpoint is not None:
        evidence.append(
            {"kind": "worker_checkpoint", "id": str(experiment_id), "hash": checkpoint.content_hash}
        )
    if lease is not None:
        evidence.append(
            _row_evidence(
                "worker_lease",
                experiment_id,
                {
                    "worker_id": lease.worker_id,
                    "status": lease.status,
                    "heartbeat_at": lease.heartbeat_at,
                    "expires_at": lease.expires_at,
                    "released_at": lease.released_at,
                    "epoch": lease.epoch,
                },
            )
        )
    if control is not None:
        evidence.append(
            {"kind": "trading_control", "id": str(experiment_id), "hash": control.content_hash}
        )
    evidence.extend(
        {"kind": "market_cursor", "id": str(row.id), "hash": row.content_hash}
        for row in cursor_rows
    )
    evidence.extend(
        _row_evidence(
            "open_position",
            row.id,
            {
                "symbol": row.symbol,
                "quantity": row.quantity,
                "mark_price": row.mark_price,
                "version": row.version,
            },
        )
        for row in open_position_rows
    )
    evidence.extend(
        {"kind": "pending_order", "id": str(row.id), "hash": row.content_hash}
        for row in pending_order_rows
    )
    evidence.extend(
        {"kind": "unresolved_counterfactual", "id": str(row.id), "hash": row.content_hash}
        for row in unresolved_counterfactual_rows
    )
    evidence.extend(
        {"kind": "operator_review_incident", "id": str(row.id), "hash": row.content_hash}
        for row in operator_review_rows
    )
    evidence.sort(key=lambda item: (str(item["kind"]), str(item["id"])))
    authoritative_hash = content_hash(evidence)

    agent_reasons = [reason for row in agent_rows for reason in row.reason_codes_json]
    required_quality_failures = sum(
        1 for row in quality_rows if row.required and row.status == "failed"
    )
    account = {
        "starting_equity": start_account["equity"],
        "ending_equity": end_account["equity"],
        "net_change": cast(Decimal, end_account["equity"]) - cast(Decimal, start_account["equity"]),
        "cash_balance": end_account["cash_balance"],
        "realized_pnl": cast(Decimal, end_account["realized_pnl"])
        - cast(Decimal, start_account["realized_pnl"]),
        "ending_realized_pnl": end_account["realized_pnl"],
        "unrealized_pnl": end_account["unrealized_pnl"],
        "fees": cast(Decimal, end_account["fees"]) - cast(Decimal, start_account["fees"]),
        "ending_fees": end_account["fees"],
        "funding": cast(Decimal, end_account["funding"]) - cast(Decimal, start_account["funding"]),
        "ending_funding": end_account["funding"],
        "maximum_drawdown": max_drawdown,
        "peak_exposure": peak_exposure,
        "peak_risk_at_stop": peak_risk_at_stop,
        "peak_used_margin": peak_margin,
        "start_source": start_account["source"],
        "end_source": end_account["source"],
        "start_snapshot_at": start_account["snapshot_at"],
        "end_snapshot_at": end_account["snapshot_at"],
    }
    research_dataset = await load_research_dataset(session, experiment, cutoff=cutoff)
    analytics = {
        "scope": "experiment_to_cutoff",
        "as_of": research_dataset.analytics_as_of,
        **research_dataset.analytics,
    }
    report: dict[str, object] = {
        "report_type": "daily",
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "generated_at": _iso(generated_at),
        "complete_day": generated_at >= window.end_utc,
        "safety": {
            "paper_trading_only": True,
            "live_money": False,
            "authenticated_exchange_credentials_used": False,
        },
        "experiment": {
            "id": experiment.id,
            "name": experiment.name,
            "mode": experiment.mode,
            "status": experiment.status,
            "git_sha": experiment.git_sha,
            "worktree_hash": experiment.worktree_hash,
            "lock_hash": experiment.lock_hash,
            "schema_revision": experiment.schema_revision,
            "config_hash": experiment.config_hash,
            "manifest_hash": experiment.manifest_hash,
            "started_at": _iso(experiment.started_at)
            if experiment.started_at is not None
            else None,
        },
        "window": {
            "timezone": "Europe/Berlin",
            "start_local": window.start_local.isoformat(),
            "end_local": window.end_local.isoformat(),
            "start_utc": _iso(window.start_utc),
            "end_utc": _iso(window.end_utc),
            "cutoff_utc": _iso(cutoff),
        },
        "account": account,
        "analytics": analytics,
        "model_assumptions": _report_model_assumptions(experiment.manifest_json),
        "decisions": {
            "total": len(decision_rows),
            "by_status": _counts([row.status for row in decision_rows]),
            "by_disposition": _counts([row.disposition for row in decision_rows]),
            "by_direction": _counts([row.direction for row in decision_rows]),
            "by_reason": _counts([row.reason_code for row in decision_rows]),
            "by_symbol": _counts([row.symbol for row in decision_rows]),
            "by_regime": _counts([row.regime for row in decision_rows]),
        },
        "decision_index": [
            {
                "id": row.id,
                "cycle_at": row.cycle_at,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "regime": row.regime,
                "status": row.status,
                "direction": row.direction,
                "disposition": row.disposition,
                "reason_code": row.reason_code,
                "market_frame_id": row.market_frame_id,
                "content_hash": row.content_hash,
            }
            for row in decision_rows
        ],
        "agents": {
            "evaluations": len(agent_rows),
            "by_name": _counts([row.agent_name for row in agent_rows]),
            "by_maturity": _counts([row.maturity for row in agent_rows]),
            "by_direction": _counts([row.direction for row in agent_rows]),
            "by_reason": _counts(agent_reasons),
            "incompatible": sum(1 for row in agent_rows if not row.compatible),
            "disabled": sum(1 for row in agent_rows if not row.enabled),
        },
        "gates": {
            "evaluations": len(gate_rows),
            "passed": sum(1 for row in gate_rows if row.passed),
            "failed": sum(1 for row in gate_rows if not row.passed),
            "by_type": _counts([row.gate_type for row in gate_rows]),
            "failures_by_reason": _counts([row.reason_code for row in gate_rows if not row.passed]),
        },
        "data_quality": {
            "evaluations": len(quality_rows),
            "by_status": _counts([row.status for row in quality_rows]),
            "by_check": _counts([row.check_name for row in quality_rows]),
            "by_reason": _counts([row.reason_code for row in quality_rows]),
            "failed_required": required_quality_failures,
        },
        "execution": {
            "proposals": len(proposal_rows),
            "proposals_by_status": _counts([row.status for row in proposal_rows]),
            "orders_created": len(order_rows),
            "orders_by_status": _counts([row.status for row in order_rows]),
            "order_events": len(order_event_rows),
            "order_events_by_type": _counts([row.event_type for row in order_event_rows]),
            "fills": len(fill_rows),
            "filled_quantity": _sum([row.quantity for row in fill_rows]),
            "fees": _sum([row.fee for row in fill_rows]),
            "spread_cost": _sum([row.spread_cost for row in fill_rows]),
            "depth_slippage": _sum([row.depth_slippage for row in fill_rows]),
            "latency_slippage": _sum([row.latency_slippage for row in fill_rows]),
            "total_slippage": _sum([row.total_slippage for row in fill_rows]),
            "funding_entries": len(funding_rows),
            "funding_amount": _sum([row.amount for row in funding_rows]),
        },
        "execution_index": [
            *(
                {
                    "record_type": "proposal",
                    "id": row.id,
                    "event_at": row.proposed_at,
                    "symbol": row.symbol,
                    "status": row.status,
                    "direction": row.direction,
                    "reason_code": row.reason_code,
                    "decision_cycle_id": row.decision_cycle_id,
                    "approved_notional": row.approved_notional,
                }
                for row in proposal_rows
            ),
            *(
                {
                    "record_type": "order",
                    "id": row.id,
                    "event_at": row.created_at,
                    "symbol": row.symbol,
                    "status": row.status,
                    "side": row.side,
                    "position_effect": row.position_effect,
                    "proposal_id": row.proposal_id,
                    "quantity": row.quantity,
                    "filled_quantity": row.filled_quantity,
                }
                for row in order_rows
            ),
            *(
                {
                    "record_type": "order_event",
                    "id": row.id,
                    "event_at": row.event_at,
                    "status": row.event_type,
                    "order_intent_id": row.order_intent_id,
                }
                for row in order_event_rows
            ),
            *(
                {
                    "record_type": "fill",
                    "id": row.id,
                    "event_at": row.fill_at,
                    "order_intent_id": row.order_intent_id,
                    "quantity": row.quantity,
                    "price": row.price,
                    "fee": row.fee,
                    "spread_cost": row.spread_cost,
                    "depth_slippage": row.depth_slippage,
                    "latency_slippage": row.latency_slippage,
                    "total_slippage": row.total_slippage,
                }
                for row in fill_rows
            ),
            *(
                {
                    "record_type": "funding",
                    "id": row.id,
                    "event_at": row.funding_at,
                    "funding_amount": row.amount,
                }
                for row in funding_rows
            ),
        ],
        "counterfactuals": {
            "created": len(counterfactual_rows),
            "by_status": _counts([row.status for row in counterfactual_rows]),
            "by_rejection_gate": _counts([row.rejection_gate for row in counterfactual_rows]),
            "resolved_pnl": _sum(
                [
                    row.hypothetical_pnl
                    for row in counterfactual_rows
                    if row.hypothetical_pnl is not None
                ]
            ),
        },
        "operations": {
            "incidents_detected": len(incident_rows),
            "incidents_by_severity": _counts([row.severity for row in incident_rows]),
            "incidents_by_reason": _counts([row.reason_code for row in incident_rows]),
            "operator_review_open": len(operator_review_rows),
            "data_quality_failed_required": required_quality_failures,
            "recoveries_started": len(recovery_rows),
            "recoveries_by_status": _counts([row.status for row in recovery_rows]),
            "worker_restarts": len(restart_rows),
        },
        "operator_actions": {
            "events": len(operator_action_rows),
            "requests": sum(
                1 for row in operator_action_rows if row.event_type == "operator_command.requested"
            ),
            "rejections": sum(
                1 for row in operator_action_rows if row.event_type == "operator_command.rejected"
            ),
            "recoveries": sum(
                1 for row in operator_action_rows if row.event_type == "operator_command.recovered"
            ),
            "by_event_type": _counts([row.event_type for row in operator_action_rows]),
            "by_command_type": _counts(
                [str(row.payload_json.get("command_type")) for row in operator_action_rows]
            ),
            "by_status": _counts(
                [str(row.payload_json.get("status")) for row in operator_action_rows]
            ),
        },
        "operator_action_index": [
            {
                "event_id": row.id,
                "global_position": row.global_position,
                "command_id": row.aggregate_id,
                "stream_version": row.stream_version,
                "event_type": row.event_type,
                "event_version": row.event_version,
                "event_at": row.occurred_at,
                "recorded_at": row.recorded_at,
                "command_type": row.payload_json.get("command_type"),
                "status": row.payload_json.get("status"),
                "idempotency_key": row.payload_json.get("idempotency_key"),
                "actor": row.payload_json.get("actor"),
                "reason": row.payload_json.get("reason"),
                "payload": row.payload_json.get("payload"),
                "operator_confirmed": row.payload_json.get("operator_confirmed"),
                "request_hash": row.payload_json.get("request_hash"),
                "requested_at": row.payload_json.get("requested_at"),
                "accepted_at": row.payload_json.get("accepted_at"),
                "accepted_by": row.payload_json.get("accepted_by"),
                "completed_at": row.payload_json.get("completed_at"),
                "result": row.payload_json.get("result"),
                "version": row.payload_json.get("version"),
                "metadata": row.metadata_json,
            }
            for row in operator_action_rows
        ],
        "runtime_snapshot": {
            "as_of": _iso(generated_at),
            "worker_status": checkpoint.status if checkpoint is not None else None,
            "checkpoint_at": checkpoint.checkpoint_at if checkpoint is not None else None,
            "checkpoint_version": checkpoint.version if checkpoint is not None else None,
            "lease_status": lease.status if lease is not None else None,
            "lease_heartbeat_at": lease.heartbeat_at if lease is not None else None,
            "lease_expires_at": lease.expires_at if lease is not None else None,
            "lease_epoch": lease.epoch if lease is not None else None,
            "kill_switch_active": control.kill_switch_active if control is not None else False,
            "kill_switch_reason": control.reason if control is not None else None,
            "open_positions": len(open_position_rows),
            "pending_orders": len(pending_order_rows),
            "unresolved_counterfactuals": len(unresolved_counterfactual_rows),
            "expected_symbols": expected_symbols,
            "cursor_count": len(cursor_rows),
            "latest_bar_close_at": latest_bar_close_at,
            "latest_cursor_update_at": latest_cursor_update_at,
            "halted_cursors": sum(1 for row in cursor_rows if row.status == "halted"),
        },
        "reconciliation": {
            "scope": "report experiment rows plus global ledger consistency",
            "ledger_ok": ledger.ok,
            "ledger_error_count": len(ledger.errors),
            "ledger_error_codes": _counts([error.code for error in ledger.errors]),
            "authoritative_record_count": len(evidence),
            "authoritative_hash": authoritative_hash,
            "analytics_hash": content_hash(analytics),
        },
    }
    report_id = content_hash(
        {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "report_date": report_date.isoformat(),
            "cutoff": cutoff,
            "authoritative_hash": authoritative_hash,
            "analytics_hash": content_hash(analytics),
        }
    )
    report["report_id"] = report_id
    report_hash = content_hash(report)
    cast(dict[str, object], report["reconciliation"])["report_hash"] = report_hash
    normalized = to_json_data(report)
    if not isinstance(normalized, dict):
        raise TypeError("daily report must normalize to a JSON object")
    return cast(dict[str, object], normalized)


async def build_configured_daily_report(
    experiment_id: UUID,
    report_date: date,
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    engine = create_async_engine(get_settings().database_url_value, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await establish_read_only_snapshot(session)
                report = await build_daily_report(
                    session,
                    experiment_id,
                    report_date,
                    generated_at=generated_at or datetime.now(UTC),
                )
        return report
    finally:
        await engine.dispose()


def _markdown_table(values: dict[str, object]) -> str:
    if not values:
        return "_None_"
    return "\n".join(f"| {key} | {value} |" for key, value in values.items())


def _markdown_cell(value: object) -> str:
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        rendered = str(value if value is not None else "—")
    return rendered.replace("|", "\\|").replace("\n", " ")


def _operator_action_trail(rows: object) -> str:
    if not isinstance(rows, list) or not rows:
        return "| _No operator actions_ |  |  |  |  |  |  |  |"
    rendered: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rendered.append(
            "| {event_at} | {command_type} | {event_type} | {actor} | {accepted_by} | "
            "{reason} | `{request_hash}` | {result} |".format(
                **{
                    key: _markdown_cell(row.get(key))
                    for key in (
                        "event_at",
                        "command_type",
                        "event_type",
                        "actor",
                        "accepted_by",
                        "reason",
                        "request_hash",
                        "result",
                    )
                }
            )
        )
    return "\n".join(rendered) if rendered else "| _No operator actions_ |  |  |  |  |  |  |  |"


def render_daily_report_markdown(report: dict[str, object]) -> str:
    experiment = cast(dict[str, object], report["experiment"])
    account = cast(dict[str, object], report["account"])
    analytics = cast(dict[str, object], report["analytics"])
    performance = cast(dict[str, object], analytics["performance"])
    calibration = cast(dict[str, object], analytics["calibration"])
    consensus_calibration = cast(dict[str, object], calibration.get("consensus", {}))
    benchmarks = cast(dict[str, object], analytics["benchmarks"])
    buy_and_hold = cast(dict[str, object], benchmarks.get("buy_and_hold", {}))
    flat_cash = cast(dict[str, object], benchmarks.get("flat_cash", {}))
    assumptions = cast(dict[str, object], report["model_assumptions"])
    margin = cast(dict[str, object], assumptions["margin"])
    decisions = cast(dict[str, object], report["decisions"])
    execution = cast(dict[str, object], report["execution"])
    operations = cast(dict[str, object], report["operations"])
    operator_actions = cast(dict[str, object], report["operator_actions"])
    runtime = cast(dict[str, object], report["runtime_snapshot"])
    reconciliation = cast(dict[str, object], report["reconciliation"])
    reasons = cast(dict[str, object], decisions.get("by_reason", {}))
    symbols = cast(dict[str, object], decisions.get("by_symbol", {}))
    ledger_status = "PASS" if reconciliation["ledger_ok"] is True else "FAIL"
    complete = "complete" if report.get("complete_day", True) else "partial"
    return f"""# MAAIS Daily Paper Report — {report["report_date"]}

> **PAPER TRADING / NO LIVE MONEY** — This report contains simulated execution only.

Report status: **{complete}**  
Experiment: `{experiment["name"]}` (`{experiment["id"]}`)  
Generated: `{report["generated_at"]}`

## Account

| Metric | Value |
| --- | ---: |
| Starting equity | {account["starting_equity"]} |
| Ending equity | {account["ending_equity"]} |
| Net change | {account["net_change"]} |
| Realized P&L | {account["realized_pnl"]} |
| Unrealized P&L | {account["unrealized_pnl"]} |
| Fees | {account["fees"]} |
| Funding | {account["funding"]} |
| Maximum drawdown | {account["maximum_drawdown"]} |

## Performance analytics (experiment to cutoff)

| Metric | Value |
| --- | ---: |
| Closed trade allocations | {performance["closed_trade_allocations"]} |
| Win rate | {performance["win_rate"]} |
| Average win | {performance["average_win"]} |
| Average loss | {performance["average_loss"]} |
| Expectancy | {performance["expectancy"]} |
| Profit factor | {performance["profit_factor"]} |
| Average R multiple | {performance["average_r_multiple"]} |
| Maximum favorable excursion | {performance["maximum_favorable_excursion"]} |
| Maximum adverse excursion | {performance["maximum_adverse_excursion"]} |
| Consensus Brier score | {consensus_calibration.get("brier_score", "—")} |
| Buy and hold ending equity | {buy_and_hold.get("ending_equity", "—")} |
| Flat cash ending equity | {flat_cash.get("ending_equity", "—")} |

## Model assumptions and limitations

| Assumption | Value |
| --- | --- |
| Leverage | {margin["leverage"]}x |
| Maintenance margin model | {margin["maintenance_margin_model"]} |
| Maintenance margin rate | {margin["maintenance_margin_rate"]} |
| Liquidation price model | {str(margin["liquidation_price_model"]).replace("_", " ").upper()} |
| Exchange liquidation parity | {"YES" if margin["exchange_liquidation_parity"] else "NO"} |

Paper results do not reproduce exchange liquidation behavior. Maintenance margin is a
frozen paper-model assumption; no exchange liquidation price is estimated.

## Decisions

Total decisions: **{decisions["total"]}**

| Reason | Count |
| --- | ---: |
{_markdown_table(reasons)}

| Symbol | Count |
| --- | ---: |
{_markdown_table(symbols)}

## Execution

| Metric | Value |
| --- | ---: |
| Proposals | {execution["proposals"]} |
| Orders created | {execution["orders_created"]} |
| Fills | {execution["fills"]} |
| Filled quantity | {execution["filled_quantity"]} |
| Fees | {execution["fees"]} |
| Spread cost | {execution["spread_cost"]} |
| Depth slippage | {execution["depth_slippage"]} |
| Latency slippage | {execution["latency_slippage"]} |
| Total slippage | {execution["total_slippage"]} |

## Operations

| Metric | Value |
| --- | ---: |
| Incidents detected | {operations["incidents_detected"]} |
| Operator-review incidents open | {operations["operator_review_open"]} |
| Required data-quality failures | {operations["data_quality_failed_required"]} |
| Recovery runs started | {operations["recoveries_started"]} |
| Worker starts/restarts | {operations["worker_restarts"]} |
| Worker status | {runtime["worker_status"]} |
| Lease status | {runtime["lease_status"]} |
| Kill switch active | {runtime["kill_switch_active"]} |
| Open positions | {runtime["open_positions"]} |
| Pending orders | {runtime["pending_orders"]} |

## Operator actions

| Metric | Value |
| --- | ---: |
| Lifecycle events | {operator_actions["events"]} |
| Requests | {operator_actions["requests"]} |
| Rejections | {operator_actions["rejections"]} |
| Crash recoveries | {operator_actions["recoveries"]} |

### Operator action trail

| Event time | Command | Lifecycle | Actor | Worker | Reason | Request hash | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
{_operator_action_trail(report.get("operator_action_index"))}

## Reconciliation

| Check | Result |
| --- | --- |
| Ledger consistency | {ledger_status} |
| Ledger errors | {reconciliation["ledger_error_count"]} |
| Authoritative records | {reconciliation["authoritative_record_count"]} |
| Authoritative hash | `{reconciliation["authoritative_hash"]}` |
| Report hash | `{reconciliation["report_hash"]}` |

## Reproducibility

| Identity | Value |
| --- | --- |
| Git SHA | `{experiment["git_sha"]}` |
| Manifest hash | `{experiment["manifest_hash"]}` |
| Schema revision | `{experiment["schema_revision"]}` |
| Report ID | `{report["report_id"]}` |
"""


_DECISION_EXPORT_SCHEMA = (
    ("id", "VARCHAR"),
    ("cycle_at", "TIMESTAMP"),
    ("symbol", "VARCHAR"),
    ("timeframe", "VARCHAR"),
    ("regime", "VARCHAR"),
    ("status", "VARCHAR"),
    ("direction", "VARCHAR"),
    ("disposition", "VARCHAR"),
    ("reason_code", "VARCHAR"),
    ("market_frame_id", "VARCHAR"),
    ("content_hash", "VARCHAR"),
)
_EXECUTION_EXPORT_SCHEMA = (
    ("record_type", "VARCHAR"),
    ("id", "VARCHAR"),
    ("event_at", "TIMESTAMP"),
    ("symbol", "VARCHAR"),
    ("status", "VARCHAR"),
    ("direction", "VARCHAR"),
    ("side", "VARCHAR"),
    ("position_effect", "VARCHAR"),
    ("reason_code", "VARCHAR"),
    ("decision_cycle_id", "VARCHAR"),
    ("proposal_id", "VARCHAR"),
    ("order_intent_id", "VARCHAR"),
    ("quantity", "DECIMAL(38,18)"),
    ("filled_quantity", "DECIMAL(38,18)"),
    ("approved_notional", "DECIMAL(38,18)"),
    ("price", "DECIMAL(38,18)"),
    ("fee", "DECIMAL(38,18)"),
    ("spread_cost", "DECIMAL(38,18)"),
    ("depth_slippage", "DECIMAL(38,18)"),
    ("latency_slippage", "DECIMAL(38,18)"),
    ("total_slippage", "DECIMAL(38,18)"),
    ("funding_amount", "DECIMAL(38,18)"),
)


def _export_rows(
    report: dict[str, object],
    key: str,
    schema: tuple[tuple[str, str], ...],
) -> list[dict[str, object]]:
    raw_rows = report.get(key, [])
    if not isinstance(raw_rows, list):
        raise TypeError(f"{key} must be a list")
    columns = tuple(column for column, _data_type in schema)
    rows: list[dict[str, object]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise TypeError(f"{key} entries must be objects")
        rows.append({column: raw_row.get(column) for column in columns})
    return rows


def _write_csv(
    path: Path,
    rows: list[dict[str, object]],
    schema: tuple[tuple[str, str], ...],
) -> None:
    columns = [column for column, _data_type in schema]
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_parquet(
    path: Path,
    rows: list[dict[str, object]],
    schema: tuple[tuple[str, str], ...],
) -> None:
    columns = [column for column, _data_type in schema]
    definitions = ", ".join(f'"{column}" {data_type}' for column, data_type in schema)
    placeholders = ", ".join("?" for _column in columns)
    escaped_path = path.as_posix().replace("'", "''")
    with duckdb.connect() as connection:
        connection.execute(f"CREATE TABLE export_rows ({definitions})")
        if rows:
            connection.executemany(
                f"INSERT INTO export_rows VALUES ({placeholders})",
                [[row[column] for column in columns] for row in rows],
            )
        connection.execute(
            f"COPY export_rows TO '{escaped_path}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_daily_report_bundle(
    report: dict[str, object],
    output_directory: Path,
) -> ReportBundlePaths:
    experiment = cast(dict[str, object], report.get("experiment"))
    experiment_id = UUID(str(experiment.get("id")))
    if experiment_id.int == 0:
        raise ValueError("report requires a non-zero experiment UUID")
    report_id = str(report.get("report_id", ""))
    if len(report_id) != 64 or any(character not in "0123456789abcdef" for character in report_id):
        raise ValueError("report_id must be a lowercase SHA-256 digest")
    report_date = date.fromisoformat(str(report.get("report_date")))
    bundle_name = f"{report_date.isoformat()}-{str(experiment_id)[:8]}-{report_id[:12]}"
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / bundle_name
    if target.exists():
        raise FileExistsError(f"report bundle already exists: {target}")
    with tempfile.TemporaryDirectory(prefix=".maais-report-", dir=output_directory) as temporary:
        temporary_path = Path(temporary)
        json_path = temporary_path / "report.json"
        markdown_path = temporary_path / "report.md"
        decisions_csv_path = temporary_path / "decisions.csv"
        decisions_parquet_path = temporary_path / "decisions.parquet"
        execution_csv_path = temporary_path / "execution.csv"
        execution_parquet_path = temporary_path / "execution.parquet"
        manifest_path = temporary_path / "bundle-manifest.json"
        json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(
            render_daily_report_markdown(report),
            encoding="utf-8",
        )
        decision_rows = _export_rows(report, "decision_index", _DECISION_EXPORT_SCHEMA)
        execution_rows = _export_rows(report, "execution_index", _EXECUTION_EXPORT_SCHEMA)
        _write_csv(decisions_csv_path, decision_rows, _DECISION_EXPORT_SCHEMA)
        _write_parquet(decisions_parquet_path, decision_rows, _DECISION_EXPORT_SCHEMA)
        _write_csv(execution_csv_path, execution_rows, _EXECUTION_EXPORT_SCHEMA)
        _write_parquet(execution_parquet_path, execution_rows, _EXECUTION_EXPORT_SCHEMA)
        artifact_paths = (
            json_path,
            markdown_path,
            decisions_csv_path,
            decisions_parquet_path,
            execution_csv_path,
            execution_parquet_path,
        )
        manifest_path.write_text(
            json.dumps(
                {
                    "report_id": report_id,
                    "report_schema_version": report.get("report_schema_version"),
                    "artifacts": {
                        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
                        for path in artifact_paths
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(target)
    return ReportBundlePaths(
        directory=target,
        json_path=target / "report.json",
        markdown_path=target / "report.md",
        decisions_csv_path=target / "decisions.csv",
        decisions_parquet_path=target / "decisions.parquet",
        execution_csv_path=target / "execution.csv",
        execution_parquet_path=target / "execution.parquet",
        manifest_path=target / "bundle-manifest.json",
    )
