from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from uuid import UUID

from maais.config.constants import ALL_AGENTS
from maais.execution.paper.account import AccountState
from maais.execution.paper.exits import ExitPlan
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import BookSnapshot
from maais.experiments.manifest import ExperimentManifest
from maais.market_data.frames import CausalMinuteFrame
from maais.market_data.integrity.state_machine import IntegrityAssessment
from maais.monitoring.admission import MonitoringAdmissionContext
from maais.risk.official import CorrelationObservation, DrawdownSnapshot, OpenRiskPosition


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")


@dataclass(frozen=True, slots=True)
class EntryDecisionContext:
    monitoring: MonitoringAdmissionContext
    drawdown: DrawdownSnapshot
    open_positions: tuple[OpenRiskPosition, ...]
    correlations: tuple[CorrelationObservation, ...]
    exchange_filters: ExchangeFilterSnapshot
    account: AccountState
    books: tuple[BookSnapshot, ...]
    active_exit_plan: ExitPlan | None
    proposal_ttl: timedelta
    execution_latency: timedelta
    taker_fee_rate: Decimal

    def __post_init__(self) -> None:
        if self.proposal_ttl <= timedelta(0) or self.execution_latency <= timedelta(0):
            raise ValueError("proposal TTL and execution latency must be positive")
        if (
            not self.taker_fee_rate.is_finite()
            or self.taker_fee_rate < 0
            or self.taker_fee_rate > 1
        ):
            raise ValueError("taker fee rate must be a finite Decimal in [0, 1]")
        if (
            self.drawdown.peak_equity != self.account.peak_equity
            or self.drawdown.current_equity != self.account.equity
        ):
            raise ValueError("drawdown snapshot must reconcile to the paper account")
        account_positions = {
            symbol: position
            for symbol, position in self.account.positions.items()
            if not position.is_flat
        }
        risk_positions = {position.symbol: position for position in self.open_positions}
        if set(account_positions) != set(risk_positions):
            raise ValueError("open risk positions must cover every non-flat paper position")
        for symbol, position in account_positions.items():
            risk_position = risk_positions[symbol]
            if (
                risk_position.notional != position.gross_notional
                or risk_position.margin != position.gross_notional / Decimal(self.account.leverage)
                or risk_position.loss_at_stop <= 0
            ):
                raise ValueError("open risk position does not reconcile to the paper account")
        current = account_positions.get(self.exchange_filters.symbol)
        if current is not None and self.active_exit_plan is None:
            raise ValueError("a non-flat paper position requires its active exit plan")
        if self.active_exit_plan is not None:
            position = self.account.position(self.exchange_filters.symbol)
            if self.active_exit_plan.position_id != position.position_id:
                raise ValueError("active exit plan belongs to another paper position")
        object.__setattr__(
            self,
            "open_positions",
            tuple(sorted(self.open_positions, key=lambda item: item.symbol)),
        )
        object.__setattr__(
            self,
            "correlations",
            tuple(sorted(self.correlations, key=lambda item: item.other_symbol)),
        )
        object.__setattr__(
            self,
            "books",
            tuple(sorted(self.books, key=lambda item: (item.observed_at, item.sequence))),
        )


@dataclass(frozen=True, slots=True)
class OrchestrationCommand:
    """All immutable identities required to decide one causal frame."""

    frame: CausalMinuteFrame
    integrity: IntegrityAssessment
    manifest: ExperimentManifest
    agent_version_ids: Mapping[str, UUID]
    evaluated_at: datetime
    completed_at: datetime
    entry_context: EntryDecisionContext | None = None

    def __post_init__(self) -> None:
        _require_utc(self.evaluated_at, "evaluated_at")
        _require_utc(self.completed_at, "completed_at")
        if self.integrity.frame_id != self.frame.frame_id:
            raise ValueError("integrity assessment belongs to another frame")
        if self.manifest.experiment_id != self.frame.key.experiment_id:
            raise ValueError("manifest and frame experiment differ")
        if self.frame.key.symbol not in self.manifest.symbols:
            raise ValueError("frame symbol is not pinned by the experiment manifest")
        if self.evaluated_at < self.frame.cutoff_at or self.completed_at < self.evaluated_at:
            raise ValueError("orchestration times are not causally ordered")
        context = self.entry_context
        if context is not None:
            if (
                context.monitoring.symbol != self.frame.key.symbol
                or context.monitoring.timeframe != self.frame.key.timeframe
                or context.exchange_filters.symbol != self.frame.key.symbol
                or context.account.experiment_id != self.frame.key.experiment_id
            ):
                raise ValueError("entry context identity differs from the causal frame")
            if context.monitoring.evaluated_at != self.evaluated_at:
                raise ValueError("monitoring and orchestration evaluation times differ")
            if context.exchange_filters.captured_at > self.completed_at:
                raise ValueError("exchange filters cannot be captured after the decision")
            raw_taker_fee = self.manifest.fee_policy.get("taker")
            try:
                manifest_taker_fee = Decimal(str(raw_taker_fee))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("manifest taker fee must be an explicit decimal") from exc
            if not manifest_taker_fee.is_finite() or (context.taker_fee_rate != manifest_taker_fee):
                raise ValueError("entry-context taker fee differs from the frozen manifest")
        version_ids = dict(self.agent_version_ids)
        if set(version_ids) != set(ALL_AGENTS) or any(
            value.int == 0 for value in version_ids.values()
        ):
            raise ValueError("agent version IDs must contain exactly eight non-nil identities")
        object.__setattr__(self, "agent_version_ids", MappingProxyType(version_ids))

    @property
    def feature_version(self) -> str:
        version = self.manifest.component_versions.get("features")
        if not isinstance(version, str) or not version:
            raise ValueError("manifest must pin a feature version")
        return version
