from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from maais.domain.enums import Direction
from maais.execution.paper.account import AccountState
from maais.execution.paper.broker import (
    ExitExecutionHalt,
    MarketExitCommand,
    PaperBroker,
)
from maais.execution.paper.clock import require_utc
from maais.execution.paper.exits import ExitPlan, ExitPlanStatus
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import BookSnapshot, require_positive_decimal
from maais.execution.paper.records import FundingRecord, PaperExecutionRecord
from maais.market_data.events import (
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
)
from maais.operations.incidents import IncidentSeverity, IncidentState


class ProtectionDisposition(StrEnum):
    MARKED = "marked"
    EXITED = "exited"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class ProtectionContext:
    experiment_id: UUID
    entry_proposal_id: UUID
    symbol: str
    account: AccountState
    exit_plan: ExitPlan
    exchange_filters: ExchangeFilterSnapshot
    books: tuple[BookSnapshot, ...]
    execution_latency: timedelta
    order_ttl: timedelta
    taker_fee_rate: Decimal
    entry_admission_halted: bool

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or self.entry_proposal_id.int == 0:
            raise ValueError("protection identities cannot be nil")
        if self.account.experiment_id != self.experiment_id:
            raise ValueError("protection account and experiment differ")
        if self.exchange_filters.symbol != self.symbol:
            raise ValueError("protection filter and symbol differ")
        position = self.account.position(self.symbol)
        if position.is_flat or position.position_id != self.exit_plan.position_id:
            raise ValueError("protection requires the matching non-flat position")
        if self.exit_plan.status not in {
            ExitPlanStatus.ACTIVE,
            ExitPlanStatus.TRIGGERED,
        }:
            raise ValueError("protection requires an active or triggered exit plan")
        if self.execution_latency <= timedelta(0) or self.order_ttl <= timedelta(0):
            raise ValueError("protection latency and order TTL must be positive")
        if (
            not isinstance(self.taker_fee_rate, Decimal)
            or not self.taker_fee_rate.is_finite()
            or not Decimal("0") <= self.taker_fee_rate <= Decimal("1")
        ):
            raise ValueError("protection taker fee must be a finite Decimal in [0, 1]")
        object.__setattr__(
            self,
            "books",
            tuple(sorted(self.books, key=lambda item: (item.observed_at, item.sequence))),
        )


@dataclass(frozen=True, slots=True)
class ProtectionOutcome:
    disposition: ProtectionDisposition
    market_event_id: str
    account: AccountState
    exit_plan: ExitPlan
    execution: PaperExecutionRecord | None
    incident: IncidentState | None
    requires_persistent_halt: bool

    def __post_init__(self) -> None:
        if not self.market_event_id:
            raise ValueError("protection outcome requires a market event")
        if self.disposition is ProtectionDisposition.MARKED and (
            self.execution is not None
            or self.incident is not None
            or self.requires_persistent_halt
            or self.exit_plan.status is not ExitPlanStatus.ACTIVE
        ):
            raise ValueError("marked outcome shape is invalid")
        if self.disposition is ProtectionDisposition.EXITED and (
            self.execution is None
            or self.incident is not None
            or self.requires_persistent_halt
            or self.exit_plan.status is not ExitPlanStatus.CLOSED
        ):
            raise ValueError("exited outcome shape is invalid")
        if self.disposition is ProtectionDisposition.HALTED and (
            self.execution is not None
            or self.incident is None
            or not self.requires_persistent_halt
            or self.exit_plan.status is not ExitPlanStatus.TRIGGERED
        ):
            raise ValueError("halted protection outcome shape is invalid")
        if not self.account.reconcile().ok:
            raise ValueError("protection account does not reconcile")
        if self.execution is not None and (
            self.execution.account != self.account or self.execution.exit_plan != self.exit_plan
        ):
            raise ValueError("protection execution differs from official account or exit plan")
        if self.incident is not None and self.incident.experiment_id != self.account.experiment_id:
            raise ValueError("protection incident and account experiment differ")


@dataclass(frozen=True, slots=True)
class FundingSettlementCommand:
    experiment_id: UUID
    symbol: str
    market_event_id: str
    funding_at: datetime
    observed_at: datetime
    mark_price: Decimal
    rate: Decimal
    rate_type: str
    account: AccountState

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0 or not self.symbol or not self.market_event_id:
            raise ValueError("funding settlement identity is required")
        if self.account.experiment_id != self.experiment_id:
            raise ValueError("funding account and experiment differ")
        require_utc(self.funding_at, "funding_at")
        require_utc(self.observed_at, "observed_at")
        if self.observed_at < self.funding_at:
            raise ValueError("funding cannot be observed before venue settlement")
        if self.account.updated_at is not None and self.account.updated_at > self.observed_at:
            raise ValueError("funding settlement would regress official account time")
        require_positive_decimal(self.mark_price, "funding mark_price")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite():
            raise ValueError("funding rate must be a finite Decimal")
        if self.rate_type not in {"Regular", "Special"}:
            raise ValueError("funding rate_type must be Regular or Special")
        position = self.account.position(self.symbol)
        if position.is_flat:
            raise ValueError("funding settlement requires an open position")
        if position.opened_at is None or self.funding_at < position.opened_at:
            raise ValueError("funding settlement predates the open position")


@dataclass(frozen=True, slots=True)
class FundingApplicationOutcome:
    record: FundingRecord
    account: AccountState
    observed_at: datetime
    rate_type: str

    def __post_init__(self) -> None:
        require_utc(self.observed_at, "observed_at")
        if self.record.observed_at != self.observed_at:
            raise ValueError("funding outcome and record observation times differ")
        if self.record.rate_type != self.rate_type:
            raise ValueError("funding outcome and record rate types differ")
        if self.record.experiment_id != self.account.experiment_id:
            raise ValueError("funding outcome account and record experiment differ")
        if not self.account.reconcile().ok:
            raise ValueError("funding outcome account does not reconcile")


class PositionProtectionService:
    """Pure paper-position protection that is independent of entry admission."""

    def __init__(self, broker: PaperBroker) -> None:
        self._broker = broker

    def evaluate_mark(
        self,
        event: ObservedMarketEvent,
        context: ProtectionContext,
    ) -> ProtectionOutcome:
        if event.kind is not MarketEventKind.MARK_FUNDING or not isinstance(
            event.payload,
            MarkFundingPayload,
        ):
            raise ValueError("position protection requires a mark/funding event")
        if event.symbol != context.symbol:
            raise ValueError("protection event and context symbol differ")
        if (
            context.account.updated_at is not None
            and event.observed_at < context.account.updated_at
        ):
            raise ValueError("protection mark would regress official account time")

        marked = context.account.mark(
            context.symbol,
            event.payload.mark_price,
            event.observed_at,
        )
        if context.exit_plan.status is ExitPlanStatus.TRIGGERED:
            plan = context.exit_plan
            intent = plan.pending_intent()
        else:
            executable_price = self._trigger_executable_price(event, context)
            evaluation = context.exit_plan.evaluate_mark(
                event.payload.mark_price,
                event.observed_at,
                executable_price=executable_price,
            )
            plan = evaluation.plan
            intent = evaluation.intent
        if intent is None:
            return ProtectionOutcome(
                disposition=ProtectionDisposition.MARKED,
                market_event_id=event.event_id,
                account=marked,
                exit_plan=plan,
                execution=None,
                incident=None,
                requires_persistent_halt=False,
            )

        assert plan.trigger_executable_price is not None
        command = MarketExitCommand(
            order_id=_id("protective-order", context.experiment_id, plan.plan_id, event.event_id),
            fill_id=_id("protective-fill", context.experiment_id, plan.plan_id, event.event_id),
            experiment_id=context.experiment_id,
            proposal_id=context.entry_proposal_id,
            client_order_id=f"paper-protect-{plan.plan_id}-{plan.version}",
            symbol=context.symbol,
            decision_executable_price=plan.trigger_executable_price,
            execution_latency=context.execution_latency,
            created_at=event.observed_at,
            expires_at=event.observed_at + context.order_ttl,
            taker_fee_rate=context.taker_fee_rate,
            intent=intent,
            exchange_filters=context.exchange_filters,
        )
        try:
            result = self._broker.execute_market_exit(
                command,
                account=marked,
                exit_plan=plan,
                books=context.books,
            )
        except ExitExecutionHalt as exc:
            incident = self._incident(
                context,
                event,
                reason_code="protective_exit_unfillable",
                evidence=dict(exc.event_payload()),
            )
            return ProtectionOutcome(
                disposition=ProtectionDisposition.HALTED,
                market_event_id=event.event_id,
                account=marked,
                exit_plan=plan,
                execution=None,
                incident=incident,
                requires_persistent_halt=True,
            )
        except Exception as exc:
            incident = self._incident(
                context,
                event,
                reason_code="protective_exit_execution_failed",
                evidence={
                    "reason": str(exc),
                    "error_type": type(exc).__name__,
                    "position_id": plan.position_id,
                    "exit_plan_id": plan.plan_id,
                    "requires_operator_review": True,
                },
            )
            return ProtectionOutcome(
                disposition=ProtectionDisposition.HALTED,
                market_event_id=event.event_id,
                account=marked,
                exit_plan=plan,
                execution=None,
                incident=incident,
                requires_persistent_halt=True,
            )

        if result.record.account is None or result.record.exit_plan is None:
            raise RuntimeError("protective exit did not produce account and exit state")
        return ProtectionOutcome(
            disposition=ProtectionDisposition.EXITED,
            market_event_id=event.event_id,
            account=result.record.account,
            exit_plan=result.record.exit_plan,
            execution=result.record,
            incident=None,
            requires_persistent_halt=False,
        )

    def apply_funding(
        self,
        command: FundingSettlementCommand,
    ) -> FundingApplicationOutcome:
        marked = command.account.mark(
            command.symbol,
            command.mark_price,
            command.observed_at,
        )
        position = marked.position(command.symbol)
        funded = marked.apply_funding(
            command.symbol,
            rate=command.rate,
            observed_at=command.observed_at,
        )
        next_position = funded.position(command.symbol)
        amount = next_position.funding - position.funding
        record = FundingRecord(
            id=_id("paper-funding", command.experiment_id, command.market_event_id),
            experiment_id=command.experiment_id,
            position_id=position.position_id,
            market_event_id=command.market_event_id,
            funding_at=command.funding_at,
            observed_at=command.observed_at,
            rate=command.rate,
            rate_type=command.rate_type,
            mark_price=command.mark_price,
            notional=position.gross_notional,
            amount=amount,
        )
        return FundingApplicationOutcome(
            record=record,
            account=funded,
            observed_at=command.observed_at,
            rate_type=command.rate_type,
        )

    @staticmethod
    def _trigger_executable_price(
        event: ObservedMarketEvent,
        context: ProtectionContext,
    ) -> Decimal:
        eligible = [
            book
            for book in context.books
            if book.symbol == context.symbol and book.observed_at <= event.observed_at
        ]
        if not eligible:
            assert isinstance(event.payload, MarkFundingPayload)
            return event.payload.mark_price
        book = eligible[-1]
        side = context.account.position(context.symbol).side
        return book.best_bid if side is Direction.LONG else book.best_ask

    @staticmethod
    def _incident(
        context: ProtectionContext,
        event: ObservedMarketEvent,
        *,
        reason_code: str,
        evidence: dict[str, object],
    ) -> IncidentState:
        deduplication_key = (
            f"protective-exit:{context.experiment_id}:{context.exit_plan.plan_id}:"
            f"{event.event_id}:{reason_code}"
        )
        return IncidentState.create(
            incident_id=_id("incident", deduplication_key),
            experiment_id=context.experiment_id,
            deduplication_key=deduplication_key,
            severity=IncidentSeverity.CRITICAL,
            component="position_protection",
            reason_code=reason_code,
            evidence={
                **evidence,
                "mark_event": event.to_dict(),
                "entry_admission_halted": context.entry_admission_halted,
            },
            requires_operator_review=True,
            detected_at=event.observed_at,
        )


def _id(namespace: str, *parts: object) -> UUID:
    identity = "/".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, f"maais://{namespace}/{identity}")
