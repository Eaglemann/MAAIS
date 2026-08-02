from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from maais.domain.enums import Direction, GateType, PaperOrderSide, PaperOrderType, PositionEffect
from maais.domain.json import canonical_json_bytes
from maais.execution.paper.account import AccountState
from maais.execution.paper.authorization import AuthorizationClaims, ExecutionAuthorizer
from maais.execution.paper.broker import MarketEntryCommand, MarketExitCommand, PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.execution.paper.market import BookLevel, BookSnapshot, TradePrint
from maais.execution.paper.orders import LimitQueueState, PaperOrder, advance_limit_queue
from maais.research.counterfactuals import CounterfactualState

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
KEY = b"maais frozen paper replay signing key 0001"
EXPECTED_SHA256 = (
    "4d2dec967a8fd98ba04616b834c1b247442af3b168409ba6d45bc24833e6b5cc"  # pragma: allowlist secret
)


def _book(
    event_id: str,
    observed_at: datetime,
    *,
    bid: str,
    ask: str,
    sequence: int,
) -> BookSnapshot:
    return BookSnapshot(
        event_id=event_id,
        symbol="BTCUSDT",
        venue_event_at=observed_at - timedelta(milliseconds=1),
        observed_at=observed_at,
        sequence=sequence,
        bids=(BookLevel(Decimal(bid), Decimal("2")),),
        asks=(BookLevel(Decimal(ask), Decimal("2")),),
        mark_price=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
    )


def _filters() -> ExchangeFilterSnapshot:
    return ExchangeFilterSnapshot(
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("10"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.MARKET, PaperOrderType.LIMIT),
        captured_at=NOW - timedelta(minutes=1),
    )


def _events(order: PaperOrder) -> list[dict[str, object]]:
    return [
        {
            "sequence": event.sequence,
            "event_type": event.event_type,
            "event_at": event.event_at,
            "payload": event.payload,
        }
        for event in order.events
    ]


def _run_sequence() -> bytes:
    experiment_id = UUID(int=1)
    decision_cycle_id = UUID(int=2)
    proposal_id = UUID(int=3)
    authorizer = ExecutionAuthorizer(KEY)
    claims = AuthorizationClaims(
        experiment_id=experiment_id,
        decision_cycle_id=decision_cycle_id,
        proposal_id=proposal_id,
        gate_chain_hash="a" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("0.1"),
        approved_notional=Decimal("11"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    broker = PaperBroker(
        clock=DeterministicClock(lambda: NOW),
        authorizer=authorizer,
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )
    entry_command = MarketEntryCommand(
        order_id=UUID(int=4),
        fill_id=UUID(int=5),
        position_id=UUID(int=6),
        exit_plan_id=UUID(int=7),
        experiment_id=experiment_id,
        decision_cycle_id=decision_cycle_id,
        proposal_id=proposal_id,
        gate_chain_hash=claims.gate_chain_hash,
        client_order_id="golden-market-entry",
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
        exchange_filters=_filters(),
    )
    account = AccountState.create(experiment_id, Decimal("10000"), "USDT")
    entry = broker.execute_market_entry(
        entry_command,
        account=account,
        books=(
            _book(
                "entry-book",
                NOW + timedelta(milliseconds=101),
                bid="100",
                ask="101",
                sequence=101,
            ),
        ),
    )
    assert entry.record.account is not None
    assert entry.record.exit_plan is not None

    limit_created_at = NOW + timedelta(milliseconds=199)
    limit_order = (
        PaperOrder.create(
            order_id=UUID(int=8),
            experiment_id=experiment_id,
            proposal_id=proposal_id,
            client_order_id="golden-limit-scale",
            command_hash="b" * 64,
            symbol="BTCUSDT",
            side=PaperOrderSide.BUY,
            order_type=PaperOrderType.LIMIT,
            position_effect=PositionEffect.OPEN,
            quantity=Decimal("0.2"),
            limit_price=Decimal("99"),
            reduce_only=False,
            open_quantity=Decimal("0.1"),
            created_at=limit_created_at,
            expires_at=NOW + timedelta(seconds=30),
        )
        .authorize(NOW + timedelta(milliseconds=200))
        .accept(NOW + timedelta(milliseconds=200))
    )
    resting_book = _book(
        "limit-resting-book",
        NOW + timedelta(milliseconds=200),
        bid="99",
        ask="100",
        sequence=200,
    )
    queue = LimitQueueState.from_book(
        order_id=limit_order.order_id,
        side=limit_order.side,
        limit_price=Decimal("99"),
        quantity=limit_order.quantity,
        eligible_after=resting_book.observed_at,
        expires_at=limit_order.expires_at,
        maker_fee_rate=Decimal("0.0002"),
        book=resting_book,
    )
    queue = advance_limit_queue(
        queue,
        TradePrint(
            event_id="queue-consume",
            symbol="BTCUSDT",
            venue_event_at=NOW + timedelta(milliseconds=200),
            observed_at=NOW + timedelta(milliseconds=201),
            sequence=201,
            price=Decimal("98.9"),
            quantity=Decimal("2"),
            aggressor_side="sell",
        ),
    ).state
    limit_advance = advance_limit_queue(
        queue,
        TradePrint(
            event_id="partial-limit-fill",
            symbol="BTCUSDT",
            venue_event_at=NOW + timedelta(milliseconds=202),
            observed_at=NOW + timedelta(milliseconds=203),
            sequence=203,
            price=Decimal("98.8"),
            quantity=Decimal("0.5"),
            aggressor_side="sell",
        ),
    )
    limit_fill = limit_advance.fills[0]
    limit_order = limit_order.apply_fill(limit_fill.quantity, limit_fill.fill_at)
    limit_order = limit_order.cancel(NOW + timedelta(milliseconds=204))
    account = entry.record.account.apply_fill(
        fill_id=UUID(int=9),
        position_id=UUID(int=6),
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        position_effect=PositionEffect.OPEN,
        quantity=limit_fill.quantity,
        price=limit_fill.price,
        fee=limit_fill.fee,
        fill_at=limit_fill.fill_at,
    )
    position = account.position("BTCUSDT")
    exit_plan = entry.record.exit_plan.resize(
        quantity=position.quantity,
        average_entry=position.average_entry,
        changed_at=limit_fill.fill_at,
    )

    funding_at = NOW + timedelta(hours=8)
    account = account.mark("BTCUSDT", Decimal("100"), funding_at - timedelta(milliseconds=1))
    account = account.apply_funding(
        "BTCUSDT",
        rate=Decimal("0.001"),
        observed_at=funding_at,
    )
    funding_amount = account.funding
    trigger_at = funding_at + timedelta(seconds=1)
    triggered = exit_plan.evaluate_mark(exit_plan.stop_price, trigger_at)
    assert triggered.intent is not None
    exit_result = broker.execute_market_exit(
        MarketExitCommand(
            order_id=UUID(int=10),
            fill_id=UUID(int=11),
            experiment_id=experiment_id,
            proposal_id=proposal_id,
            client_order_id="golden-stop-exit",
            symbol="BTCUSDT",
            decision_executable_price=exit_plan.stop_price,
            execution_latency=timedelta(milliseconds=100),
            created_at=trigger_at,
            expires_at=trigger_at + timedelta(seconds=30),
            taker_fee_rate=Decimal("0.0005"),
            intent=triggered.intent,
            exchange_filters=_filters(),
        ),
        account=account,
        exit_plan=triggered.plan,
        books=(
            _book(
                "stop-gap-book",
                trigger_at + timedelta(milliseconds=101),
                bid="98",
                ask="98.2",
                sequence=900,
            ),
        ),
    )
    assert exit_result.record.account is not None
    final_account = exit_result.record.account
    final_position = final_account.position("BTCUSDT")

    counterfactual = CounterfactualState.create(
        counterfactual_id=UUID(int=20),
        experiment_id=experiment_id,
        proposal_id=UUID(int=21),
        decision_cycle_id=UUID(int=22),
        symbol="BTCUSDT",
        direction=Direction.LONG,
        rejection_gate=GateType.EV,
        prior_gate_chain=(GateType.DATA_QUALITY, GateType.CONSENSUS, GateType.EV),
        quantity=Decimal("0.1"),
        decision_executable_price=Decimal("101"),
        eligible_after=NOW + timedelta(milliseconds=100),
        fee_rate=Decimal("0.0005"),
        expected_loss_fraction=Decimal("0.01"),
        expected_gain_fraction=Decimal("0.01"),
        created_at=NOW,
    ).enter(entry.fill, plan_id=UUID(int=23))
    counterfactual = counterfactual.observe_mark(
        Decimal("99"),
        entry.fill.fill_at + timedelta(minutes=15),
        market_event_id="counterfactual-mark-15m",
    )
    snapshot = final_account.snapshot()
    payload = {
        "market_entry": {
            "events": _events(entry.record.order),
            "fill": entry.record.fills[0].to_dict(),
        },
        "limit_order": {
            "events": _events(limit_order),
            "queue_ahead": limit_advance.state.queue_ahead,
            "filled_quantity": limit_advance.state.filled_quantity,
            "fill": {
                "market_event_id": limit_fill.market_event_id,
                "fill_at": limit_fill.fill_at,
                "quantity": limit_fill.quantity,
                "price": limit_fill.price,
                "fee": limit_fill.fee,
            },
        },
        "funding": {
            "observed_at": funding_at,
            "rate": Decimal("0.001"),
            "amount": funding_amount,
        },
        "stop_exit": {
            "trigger_price": triggered.intent.trigger_price,
            "events": _events(exit_result.record.order),
            "fill": exit_result.record.fills[0].to_dict(),
        },
        "rejected_counterfactual": {
            "status": counterfactual.status,
            "hypothetical_pnl": counterfactual.hypothetical_pnl,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "event_at": event.event_at,
                    "payload": event.payload,
                }
                for event in counterfactual.events
            ],
        },
        "final_account": {
            "version": final_account.version,
            "cash_balance": snapshot.cash_balance,
            "equity": snapshot.equity,
            "used_margin": snapshot.used_margin,
            "free_margin": snapshot.free_margin,
            "gross_notional": snapshot.gross_notional,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "realized_pnl": snapshot.realized_pnl,
            "fees": snapshot.fees,
            "funding": snapshot.funding,
            "peak_equity": snapshot.peak_equity,
            "drawdown": snapshot.drawdown,
            "position_quantity": final_position.quantity,
            "position_status": "closed" if final_position.is_flat else "open",
            "reconciled": final_account.reconcile().ok,
        },
    }
    return canonical_json_bytes(payload)


def test_golden_paper_sequence_is_byte_identical_and_matches_pinned_digest() -> None:
    first = _run_sequence()
    second = _run_sequence()

    assert first == second
    assert hashlib.sha256(first).hexdigest() == EXPECTED_SHA256
