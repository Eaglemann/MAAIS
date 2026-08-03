from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import PaperOrderSide, PaperOrderType
from maais.domain.json import canonical_json_bytes
from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import AuthorizationClaims, ExecutionAuthorizer
from maais.execution.paper.broker import (
    ExitExecutionHalt,
    MarketEntryCommand,
    MarketExitCommand,
    PaperBroker,
)
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import BookLevel, BookSnapshot

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
KEY = b"a deterministic replay signing key with at least 32 bytes"


def _book(event_id: str, milliseconds: int, ask: str) -> BookSnapshot:
    observed_at = NOW + timedelta(milliseconds=milliseconds)
    return BookSnapshot(
        event_id=event_id,
        symbol="BTCUSDT",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=milliseconds,
        bids=(BookLevel(Decimal("99"), Decimal("5")),),
        asks=(BookLevel(Decimal(ask), Decimal("5")),),
        mark_price=Decimal("100"),
    )


def _command() -> MarketEntryCommand:
    authorizer = ExecutionAuthorizer(KEY)
    claims = AuthorizationClaims(
        experiment_id=UUID(int=1),
        decision_cycle_id=UUID(int=2),
        proposal_id=UUID(int=3),
        gate_chain_hash="a" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("0.1"),
        approved_notional=Decimal("11"),
        issued_at=NOW - timedelta(milliseconds=1),
        expires_at=NOW + timedelta(seconds=30),
    )
    filters = ExchangeFilterSnapshot(
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("10"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.MARKET,),
        captured_at=NOW - timedelta(minutes=1),
    )
    return MarketEntryCommand(
        order_id=UUID(int=4),
        fill_id=UUID(int=5),
        position_id=UUID(int=6),
        exit_plan_id=UUID(int=7),
        experiment_id=claims.experiment_id,
        decision_cycle_id=claims.decision_cycle_id,
        proposal_id=claims.proposal_id,
        gate_chain_hash=claims.gate_chain_hash,
        client_order_id="paper-replay-1",
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        requested_quantity=Decimal("0.1"),
        approved_quantity=Decimal("0.1"),
        approved_notional=Decimal("11"),
        decision_executable_price=Decimal("100"),
        decision_completed_at=NOW,
        execution_latency=timedelta(milliseconds=100),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        taker_fee_rate=Decimal("0.0005"),
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        capability=authorizer.issue(claims, all_gates_passed=True),
        exchange_filters=filters,
    )


def _broker() -> PaperBroker:
    return PaperBroker(
        clock=DeterministicClock(lambda: NOW),
        authorizer=ExecutionAuthorizer(KEY),
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )


def test_golden_replay_is_deterministic_and_future_book_cannot_change_fill() -> None:
    command = _command()
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")
    first = _book("eligible", 101, "101")
    later = _book("later", 200, "120")

    left = _broker().execute_market_entry(command, account=account, books=(first, later))
    right = _broker().execute_market_entry(
        command,
        account=account,
        books=(first, replace(later, asks=(BookLevel(Decimal("150"), Decimal("5")),))),
    )

    assert left == right
    assert left.fill.market_event_id == "eligible"
    assert left.fill.price == Decimal("101")
    assert left.record.account is not None
    assert left.record.account.reconcile().ok
    assert left.record.exit_plan is not None
    assert left.record.exit_plan.stop_price == Decimal("99.99")
    left_events = [event.payload for event in left.record.order.events]
    right_events = [event.payload for event in right.record.order.events]
    assert canonical_json_bytes(left_events) == canonical_json_bytes(right_events)


def test_broker_rejects_capability_if_prepared_quantity_changes() -> None:
    command = replace(_command(), requested_quantity=Decimal("0.099"))
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")

    with pytest.raises(PermissionError, match="does not match"):
        _broker().execute_market_entry(
            command,
            account=account,
            books=(_book("eligible", 101, "101"),),
        )


def test_broker_rejects_capability_borrowed_from_another_gate_chain() -> None:
    command = replace(_command(), gate_chain_hash="b" * 64)
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")

    with pytest.raises(PermissionError, match="does not match"):
        _broker().execute_market_entry(
            command,
            account=account,
            books=(_book("eligible", 101, "101"),),
        )


def test_broker_rejects_future_exchange_filter_snapshot() -> None:
    command = _command()
    command = replace(
        command,
        exchange_filters=replace(
            command.exchange_filters,
            captured_at=command.decision_completed_at + timedelta(microseconds=1),
        ),
    )
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")

    with pytest.raises(ValueError, match="after the decision"):
        _broker().execute_market_entry(
            command,
            account=account,
            books=(_book("eligible", 101, "101"),),
        )


def test_stop_exit_uses_next_eligible_book_and_reconciles_gap_loss() -> None:
    command = _command()
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")
    entry = _broker().execute_market_entry(
        command,
        account=account,
        books=(_book("entry", 101, "101"),),
    )
    assert entry.record.account is not None
    assert entry.record.exit_plan is not None
    marked = entry.record.account.mark(
        "BTCUSDT",
        Decimal("100"),
        NOW + timedelta(milliseconds=999),
    )
    funded = marked.apply_funding(
        "BTCUSDT",
        rate=Decimal("0.001"),
        observed_at=NOW + timedelta(seconds=1),
    )
    trigger_at = NOW + timedelta(seconds=2)
    triggered = entry.record.exit_plan.evaluate_mark(
        entry.record.exit_plan.stop_price,
        trigger_at,
    )
    assert triggered.intent is not None
    exit_observed = trigger_at + timedelta(milliseconds=101)
    exit_book = BookSnapshot(
        event_id="stop-gap-book",
        symbol="BTCUSDT",
        venue_event_at=exit_observed - timedelta(milliseconds=1),
        observed_at=exit_observed,
        sequence=300,
        bids=(BookLevel(Decimal("98"), Decimal("5")),),
        asks=(BookLevel(Decimal("99"), Decimal("5")),),
        mark_price=Decimal("98.5"),
    )
    exit_command = MarketExitCommand(
        order_id=UUID(int=8),
        fill_id=UUID(int=9),
        experiment_id=command.experiment_id,
        proposal_id=command.proposal_id,
        client_order_id="paper-replay-exit-1",
        symbol=command.symbol,
        decision_executable_price=entry.record.exit_plan.stop_price,
        execution_latency=timedelta(milliseconds=100),
        created_at=trigger_at,
        expires_at=trigger_at + timedelta(seconds=30),
        taker_fee_rate=command.taker_fee_rate,
        intent=triggered.intent,
        exchange_filters=command.exchange_filters,
    )

    result = _broker().execute_market_exit(
        exit_command,
        account=funded,
        exit_plan=triggered.plan,
        books=(exit_book,),
    )

    assert result.fill.price == Decimal("98")
    assert result.record.account is not None
    assert result.record.account.position("BTCUSDT").is_flat
    assert result.record.account.cash_balance == Decimal("9999.68005")
    assert result.record.account.equity == result.record.account.cash_balance
    assert result.record.account.reconcile().ok
    assert result.record.exit_plan is not None
    assert result.record.exit_plan.status.value == "closed"


def test_unfillable_triggered_exit_becomes_persistable_halt_incident() -> None:
    command = _command()
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")
    entry = _broker().execute_market_entry(
        command,
        account=account,
        books=(_book("entry", 101, "101"),),
    )
    assert entry.record.account is not None
    assert entry.record.exit_plan is not None
    trigger_at = NOW + timedelta(seconds=2)
    triggered = entry.record.exit_plan.evaluate_mark(
        entry.record.exit_plan.stop_price,
        trigger_at,
    )
    assert triggered.intent is not None
    exit_command = MarketExitCommand(
        order_id=UUID(int=8),
        fill_id=UUID(int=9),
        experiment_id=command.experiment_id,
        proposal_id=command.proposal_id,
        client_order_id="paper-replay-exit-halt",
        symbol=command.symbol,
        decision_executable_price=entry.record.exit_plan.stop_price,
        execution_latency=timedelta(milliseconds=100),
        created_at=trigger_at,
        expires_at=trigger_at + timedelta(seconds=30),
        taker_fee_rate=command.taker_fee_rate,
        intent=triggered.intent,
        exchange_filters=command.exchange_filters,
    )

    with pytest.raises(ExitExecutionHalt, match="no_eligible_book") as error:
        _broker().execute_market_exit(
            exit_command,
            account=entry.record.account,
            exit_plan=triggered.plan,
            books=(),
        )

    payload = error.value.event_payload()
    assert payload["position_id"] == str(triggered.intent.position_id)
    assert payload["exit_plan_id"] == str(triggered.plan.plan_id)
    assert payload["reason"] == "no_eligible_book"
    assert payload["requires_operator_review"] is True
