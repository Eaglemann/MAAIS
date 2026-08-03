from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from maais.domain.enums import PaperOrderSide, PositionEffect
from maais.execution.paper.account import AccountState

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


@given(
    quantities=st.lists(
        st.decimals(min_value="0.001", max_value="0.05", places=3),
        min_size=1,
        max_size=12,
    ),
    prices=st.lists(
        st.decimals(min_value="10", max_value="500", places=2),
        min_size=1,
        max_size=12,
    ),
)
def test_open_and_fifo_close_sequences_always_reconcile(
    quantities: list[Decimal],
    prices: list[Decimal],
) -> None:
    account = AccountState.create(UUID(int=1), Decimal("10000"), "USDT", leverage=1)
    opened = Decimal("0")
    for index, (quantity, price) in enumerate(zip(quantities, prices), start=1):
        fee = quantity * price * Decimal("0.0005")
        account = account.apply_fill(
            fill_id=UUID(int=index),
            position_id=UUID(int=100),
            symbol="BTCUSDT",
            side=PaperOrderSide.BUY,
            position_effect=PositionEffect.OPEN,
            quantity=quantity,
            price=price,
            fee=fee,
            fill_at=NOW + timedelta(seconds=index),
        )
        opened += quantity
        assert account.reconcile().ok
        assert account.position("BTCUSDT").quantity == opened

    close_price = prices[-1]
    close_fee = opened * close_price * Decimal("0.0005")
    account = account.apply_fill(
        fill_id=UUID(int=999),
        position_id=UUID(int=100),
        symbol="BTCUSDT",
        side=PaperOrderSide.SELL,
        position_effect=PositionEffect.REDUCE,
        quantity=opened,
        price=close_price,
        fee=close_fee,
        fill_at=NOW + timedelta(minutes=1),
    )
    assert account.position("BTCUSDT").is_flat
    assert account.reconcile().ok
    assert account.equity == account.cash_balance
