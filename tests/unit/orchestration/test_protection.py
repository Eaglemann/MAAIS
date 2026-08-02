from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import ExecutionAuthorizer
from maais.execution.paper.broker import PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.market_data.events import (
    MarketEventKind,
    MarkFundingPayload,
    ObservedMarketEvent,
)
from maais.operations.incidents import IncidentSeverity
from maais.orchestration.protection import (
    FundingSettlementCommand,
    PositionProtectionService,
    ProtectionContext,
    ProtectionDisposition,
)
from tests.unit.paper.test_broker_replay import KEY, _book, _command

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _broker() -> PaperBroker:
    return PaperBroker(
        clock=DeterministicClock(lambda: NOW),
        authorizer=ExecutionAuthorizer(KEY),
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )


def _entry():
    command = _command()
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")
    result = _broker().execute_market_entry(
        command,
        account=account,
        books=(_book("entry", 101, "101"),),
    )
    assert result.record.account is not None
    assert result.record.exit_plan is not None
    return command, result.record.account, result.record.exit_plan


def _mark(event_id: str, price: Decimal, observed_at: datetime) -> ObservedMarketEvent:
    return ObservedMarketEvent(
        venue="binance_usdm",
        stream="btcusdt@markPrice@1s",
        symbol="BTCUSDT",
        event_id=event_id,
        kind=MarketEventKind.MARK_FUNDING,
        venue_event_at=observed_at - timedelta(milliseconds=2),
        observed_at=observed_at,
        sequence=int(observed_at.timestamp() * 1000),
        sequence_not_applicable_reason=None,
        payload=MarkFundingPayload(
            mark_price=price,
            index_price=price,
            funding_rate=Decimal("0.0001"),
            next_funding_at=observed_at + timedelta(hours=8),
        ),
    )


def _depth(event_id: str, observed_at: datetime, bid: str, ask: str) -> BookSnapshot:
    return BookSnapshot(
        event_id=event_id,
        symbol="BTCUSDT",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=int(observed_at.timestamp() * 1000),
        bids=(BookLevel(Decimal(bid), Decimal("5")),),
        asks=(BookLevel(Decimal(ask), Decimal("5")),),
        mark_price=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
    )


def _context(
    books: tuple[BookSnapshot, ...],
    *,
    entry_admission_halted: bool = True,
) -> ProtectionContext:
    command, account, exit_plan = _entry()
    return ProtectionContext(
        experiment_id=command.experiment_id,
        entry_proposal_id=command.proposal_id,
        symbol=command.symbol,
        account=account,
        exit_plan=exit_plan,
        exchange_filters=command.exchange_filters,
        books=books,
        execution_latency=timedelta(milliseconds=100),
        order_ttl=timedelta(seconds=30),
        taker_fee_rate=command.taker_fee_rate,
        entry_admission_halted=entry_admission_halted,
    )


def test_nontriggering_mark_updates_account_while_entries_are_halted() -> None:
    observed_at = NOW + timedelta(seconds=2)
    context = _context((_depth("before", observed_at, "99", "100"),))

    outcome = PositionProtectionService(_broker()).evaluate_mark(
        _mark("mark-safe", Decimal("100"), observed_at),
        context,
    )

    assert outcome.disposition is ProtectionDisposition.MARKED
    assert outcome.account.position("BTCUSDT").mark_price == Decimal("100")
    assert outcome.account.version == context.account.version + 1
    assert outcome.exit_plan == context.exit_plan
    assert outcome.execution is None
    assert outcome.incident is None


def test_stop_mark_exits_on_next_eligible_book_even_when_entries_are_halted() -> None:
    trigger_at = NOW + timedelta(seconds=2)
    before = _depth("trigger-book", trigger_at, "99", "100")
    eligible = _depth(
        "gap-book",
        trigger_at + timedelta(milliseconds=101),
        "98",
        "99",
    )
    context = _context((before, eligible))

    outcome = PositionProtectionService(_broker()).evaluate_mark(
        _mark("mark-stop", context.exit_plan.stop_price, trigger_at),
        context,
    )

    assert outcome.disposition is ProtectionDisposition.EXITED
    assert outcome.execution is not None
    assert outcome.execution.fills[0].market_event_id == "gap-book"
    assert outcome.execution.fills[0].price == Decimal("98")
    assert outcome.account.position("BTCUSDT").is_flat
    assert outcome.account.reconcile().ok
    assert outcome.exit_plan.status.value == "closed"
    assert outcome.incident is None


def test_future_books_cannot_change_an_already_eligible_protective_fill() -> None:
    trigger_at = NOW + timedelta(seconds=2)
    before = _depth("trigger-book", trigger_at, "99", "100")
    eligible = _depth(
        "first-eligible",
        trigger_at + timedelta(milliseconds=101),
        "98",
        "99",
    )
    future = _depth(
        "future",
        trigger_at + timedelta(milliseconds=200),
        "90",
        "91",
    )
    context = _context((before, eligible, future))
    event = _mark("mark-stop-stable", context.exit_plan.stop_price, trigger_at)
    service = PositionProtectionService(_broker())

    left = service.evaluate_mark(event, context)
    right = service.evaluate_mark(
        event,
        replace(
            context,
            books=(
                before,
                eligible,
                replace(future, bids=(BookLevel(Decimal("1"), Decimal("5")),)),
            ),
        ),
    )

    assert left == right


def test_unfillable_triggered_exit_returns_critical_persistent_halt() -> None:
    trigger_at = NOW + timedelta(seconds=2)
    before = _depth("trigger-book", trigger_at, "99", "100")
    context = _context((before,))

    outcome = PositionProtectionService(_broker()).evaluate_mark(
        _mark("mark-unfillable", context.exit_plan.stop_price, trigger_at),
        context,
    )

    assert outcome.disposition is ProtectionDisposition.HALTED
    assert outcome.requires_persistent_halt
    assert outcome.execution is None
    assert outcome.exit_plan.status.value == "triggered"
    assert outcome.incident is not None
    assert outcome.incident.severity is IncidentSeverity.CRITICAL
    assert outcome.incident.requires_operator_review
    assert outcome.incident.reason_code == "protective_exit_unfillable"
    assert outcome.incident.evidence["reason"] == "no_eligible_book"


def test_funding_uses_official_settlement_mark_time_rate_and_position_side() -> None:
    command, account, exit_plan = _entry()
    funding_at = NOW + timedelta(hours=8)
    observed_at = funding_at + timedelta(milliseconds=150)
    account = account.mark(
        command.symbol,
        Decimal("103"),
        funding_at + timedelta(milliseconds=50),
    )
    settlement = FundingSettlementCommand(
        experiment_id=command.experiment_id,
        symbol=command.symbol,
        market_event_id="funding:BTCUSDT:2026-08-02T20:00:00Z:Regular",
        funding_at=funding_at,
        observed_at=observed_at,
        mark_price=Decimal("102"),
        rate=Decimal("0.001"),
        rate_type="Regular",
        account=account,
    )

    outcome = PositionProtectionService(_broker()).apply_funding(settlement)
    position = outcome.account.position("BTCUSDT")

    assert outcome.record.funding_at == funding_at
    assert outcome.record.observed_at == observed_at
    assert outcome.observed_at == observed_at
    assert outcome.rate_type == "Regular"
    assert outcome.record.position_id == exit_plan.position_id
    assert outcome.record.notional == Decimal("10.2")
    assert outcome.record.amount == Decimal("-0.0102")
    assert position.mark_price == Decimal("102")
    assert position.funding == Decimal("-0.0102")
    assert outcome.account.reconcile().ok
