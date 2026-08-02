from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import PaperOrderSide, PaperOrderType
from maais.execution.paper.authorization import AuthorizationClaims, ExecutionAuthorizer
from maais.execution.paper.clock import DeterministicClock, ObservedEvent
from maais.execution.paper.filters import ExchangeFilterSnapshot, FilterRejection

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def test_clock_selects_only_first_event_strictly_after_eligibility() -> None:
    clock = DeterministicClock(lambda: NOW)
    eligibility = clock.eligibility(NOW, timedelta(milliseconds=100))
    events = (
        ObservedEvent("before", NOW + timedelta(milliseconds=99), 1),
        ObservedEvent("boundary", NOW + timedelta(milliseconds=100), 2),
        ObservedEvent("eligible", NOW + timedelta(milliseconds=101), 3),
        ObservedEvent("future", NOW + timedelta(milliseconds=102), 4),
    )

    assert eligibility.eligible_at == NOW + timedelta(milliseconds=100)
    assert clock.first_eligible(eligibility, events).event_id == "eligible"


def test_clock_rejects_naive_time_and_nonpositive_latency() -> None:
    clock = DeterministicClock(lambda: NOW)
    with pytest.raises(ValueError, match="UTC-aware"):
        clock.eligibility(datetime(2026, 8, 2, 12), timedelta(milliseconds=1))
    with pytest.raises(ValueError, match="positive"):
        clock.eligibility(NOW, timedelta(0))


@pytest.fixture
def filters() -> ExchangeFilterSnapshot:
    return ExchangeFilterSnapshot(
        symbol="BTCUSDT",
        status="TRADING",
        price_tick=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("10"),
        minimum_notional=Decimal("5"),
        supported_order_types=(PaperOrderType.MARKET, PaperOrderType.LIMIT),
        captured_at=NOW,
    )


def test_filters_quantize_without_increasing_approved_risk(
    filters: ExchangeFilterSnapshot,
) -> None:
    buy = filters.prepare(
        side=PaperOrderSide.BUY,
        order_type=PaperOrderType.LIMIT,
        requested_quantity=Decimal("0.1239"),
        approved_quantity=Decimal("0.124"),
        price=Decimal("60000.19"),
        approved_notional=Decimal("7440"),
    )
    sell = filters.prepare(
        side=PaperOrderSide.SELL,
        order_type=PaperOrderType.LIMIT,
        requested_quantity=Decimal("0.1239"),
        approved_quantity=Decimal("0.124"),
        price=Decimal("60000.11"),
        approved_notional=Decimal("7441"),
    )

    assert buy.quantity == Decimal("0.123")
    assert buy.price == Decimal("60000.10")
    assert sell.quantity == Decimal("0.123")
    assert sell.price == Decimal("60000.20")
    assert buy.quantity <= buy.requested_quantity <= buy.approved_quantity


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ({"status": "BREAK"}, "symbol_not_trading"),
        ({"requested_quantity": Decimal("0.0009")}, "quantity_below_minimum"),
        (
            {"requested_quantity": Decimal("11"), "approved_quantity": Decimal("11")},
            "quantity_above_maximum",
        ),
        (
            {"price": Decimal("4000"), "approved_notional": Decimal("4")},
            "notional_below_minimum",
        ),
        ({"order_type": PaperOrderType.STOP_MARKET}, "unsupported_order_type"),
    ),
)
def test_filters_fail_closed(
    filters: ExchangeFilterSnapshot,
    change: dict[str, object],
    reason: str,
) -> None:
    snapshot = replace(filters, status=str(change["status"])) if "status" in change else filters
    values: dict[str, object] = {
        "side": PaperOrderSide.BUY,
        "order_type": PaperOrderType.LIMIT,
        "requested_quantity": Decimal("0.001"),
        "approved_quantity": Decimal("0.001"),
        "price": Decimal("6000"),
        "approved_notional": Decimal("6"),
    }
    values.update({key: value for key, value in change.items() if key != "status"})
    with pytest.raises(FilterRejection, match=reason):
        snapshot.prepare(**values)  # type: ignore[arg-type]


def test_execution_capability_binds_every_material_claim() -> None:
    authorizer = ExecutionAuthorizer(b"a deterministic test key at least 32 bytes long")
    claims = AuthorizationClaims(
        experiment_id=UUID("11111111-1111-4111-8111-111111111111"),
        decision_cycle_id=UUID("22222222-2222-4222-8222-222222222222"),
        proposal_id=UUID("33333333-3333-4333-8333-333333333333"),
        gate_chain_hash="a" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("0.123"),
        approved_notional=Decimal("7380"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    capability = authorizer.issue(claims, all_gates_passed=True)

    assert authorizer.verify(capability, at=NOW + timedelta(seconds=1))
    assert not authorizer.verify(capability, at=claims.expires_at + timedelta(microseconds=1))


@pytest.mark.parametrize(
    "change",
    (
        {"experiment_id": UUID(int=10)},
        {"decision_cycle_id": UUID(int=11)},
        {"proposal_id": UUID(int=12)},
        {"gate_chain_hash": "b" * 64},
        {"symbol": "ETHUSDT"},
        {"side": PaperOrderSide.SELL},
        {"quantity": Decimal("1")},
        {"approved_notional": Decimal("7390")},
        {"issued_at": NOW - timedelta(seconds=1)},
        {"expires_at": NOW + timedelta(seconds=31)},
    ),
)
def test_execution_capability_rejects_every_tampered_claim(change: dict[str, object]) -> None:
    authorizer = ExecutionAuthorizer(b"a deterministic test key at least 32 bytes long")
    claims = AuthorizationClaims(
        experiment_id=UUID(int=1),
        decision_cycle_id=UUID(int=2),
        proposal_id=UUID(int=3),
        gate_chain_hash="a" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.BUY,
        quantity=Decimal("0.123"),
        approved_notional=Decimal("7380"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    capability = authorizer.issue(claims, all_gates_passed=True)

    tampered = replace(capability, claims=replace(claims, **change))  # type: ignore[arg-type]

    assert not authorizer.verify(tampered, at=NOW)


def test_execution_capability_is_not_issued_for_failed_gate_chain() -> None:
    authorizer = ExecutionAuthorizer(b"a deterministic test key at least 32 bytes long")
    claims = AuthorizationClaims(
        experiment_id=UUID(int=1),
        decision_cycle_id=UUID(int=2),
        proposal_id=UUID(int=3),
        gate_chain_hash="b" * 64,
        symbol="BTCUSDT",
        side=PaperOrderSide.SELL,
        quantity=Decimal("0.01"),
        approved_notional=Decimal("600"),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(PermissionError, match="gate chain"):
        authorizer.issue(claims, all_gates_passed=False)
