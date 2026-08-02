from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from maais.domain.enums import PaperOrderSide, PositionEffect
from maais.execution.paper.account import AccountState, InsufficientMargin

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _apply(
    account: AccountState,
    *,
    fill_id: int,
    side: PaperOrderSide,
    effect: PositionEffect,
    quantity: str,
    price: str,
    fee: str,
) -> AccountState:
    return account.apply_fill(
        fill_id=UUID(int=fill_id),
        position_id=UUID(int=100),
        symbol="BTCUSDT",
        side=side,
        position_effect=effect,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fill_at=NOW + timedelta(seconds=fill_id),
    )


def test_long_fifo_partial_close_reconciles_cash_equity_fees_and_margin() -> None:
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT", leverage=1)
    account = _apply(
        account,
        fill_id=1,
        side=PaperOrderSide.BUY,
        effect=PositionEffect.OPEN,
        quantity="2",
        price="100",
        fee="0.2",
    )
    account = _apply(
        account,
        fill_id=2,
        side=PaperOrderSide.BUY,
        effect=PositionEffect.OPEN,
        quantity="1",
        price="110",
        fee="0.11",
    ).mark("BTCUSDT", Decimal("105"), NOW + timedelta(seconds=3))

    assert account.cash_balance == Decimal("9999.69")
    assert account.position("BTCUSDT").average_entry == Decimal("103.3333333333333333333333333")
    assert account.unrealized_pnl == Decimal("5")
    assert account.used_margin == Decimal("315")
    assert account.equity == Decimal("10004.69")

    account = _apply(
        account,
        fill_id=4,
        side=PaperOrderSide.SELL,
        effect=PositionEffect.REDUCE,
        quantity="1.5",
        price="108",
        fee="0.162",
    )
    position = account.position("BTCUSDT")

    assert position.quantity == Decimal("1.5")
    assert position.realized_pnl == Decimal("12.0")
    assert position.average_entry == Decimal("106.6666666666666666666666667")
    assert position.unrealized_pnl == Decimal("2.0")
    assert account.cash_balance == Decimal("10011.528")
    assert account.equity == Decimal("10013.528")
    assert account.fees == Decimal("0.472")
    assert account.reconcile().ok
    assert account.reconcile().residuals == (Decimal("0"),) * 3


def test_short_fifo_and_funding_follow_position_side() -> None:
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT", leverage=1)
    account = _apply(
        account,
        fill_id=1,
        side=PaperOrderSide.SELL,
        effect=PositionEffect.OPEN,
        quantity="2",
        price="100",
        fee="0.2",
    ).mark("BTCUSDT", Decimal("90"), NOW + timedelta(seconds=2))

    assert account.unrealized_pnl == Decimal("20")
    account = account.apply_funding(
        "BTCUSDT",
        rate=Decimal("0.001"),
        observed_at=NOW + timedelta(seconds=3),
    )
    assert account.funding == Decimal("0.180")
    assert account.cash_balance == Decimal("9999.980")

    account = _apply(
        account,
        fill_id=4,
        side=PaperOrderSide.BUY,
        effect=PositionEffect.REDUCE,
        quantity="2",
        price="90",
        fee="0.18",
    )
    assert account.position("BTCUSDT").is_flat
    assert account.realized_pnl == Decimal("20")
    assert account.cash_balance == Decimal("10019.800")
    assert account.equity == account.cash_balance
    assert account.used_margin == 0
    assert account.reconcile().ok


def test_position_rejects_float_funding_amount() -> None:
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT")
    account = _apply(
        account,
        fill_id=1,
        side=PaperOrderSide.BUY,
        effect=PositionEffect.OPEN,
        quantity="1",
        price="100",
        fee="0.05",
    )

    with pytest.raises(ValueError, match="Decimal"):
        account.position("BTCUSDT").apply_funding(0.1)  # type: ignore[arg-type]


def test_account_rejects_margin_breach_reversal_and_over_close() -> None:
    account = AccountState.create(UUID(int=1), Decimal("100"), "USDT", leverage=1)
    with pytest.raises(InsufficientMargin):
        _apply(
            account,
            fill_id=1,
            side=PaperOrderSide.BUY,
            effect=PositionEffect.OPEN,
            quantity="2",
            price="100",
            fee="0.2",
        )

    account = _apply(
        account,
        fill_id=2,
        side=PaperOrderSide.BUY,
        effect=PositionEffect.OPEN,
        quantity="0.5",
        price="100",
        fee="0.05",
    )
    with pytest.raises(ValueError, match="same direction"):
        _apply(
            account,
            fill_id=3,
            side=PaperOrderSide.SELL,
            effect=PositionEffect.OPEN,
            quantity="0.1",
            price="100",
            fee="0.01",
        )
    with pytest.raises(ValueError, match="open quantity"):
        _apply(
            account,
            fill_id=4,
            side=PaperOrderSide.SELL,
            effect=PositionEffect.REDUCE,
            quantity="0.6",
            price="100",
            fee="0.06",
        )


@pytest.mark.parametrize("leverage", (0, 6))
def test_account_hard_rejects_leverage_outside_system_bounds(leverage: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 5"):
        AccountState.create(UUID(int=1), Decimal("10000"), "USDT", leverage=leverage)
