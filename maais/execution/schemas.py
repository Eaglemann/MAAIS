"""Execution Engine schemas."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"  # Stop-Market
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass
class OrderRequest:
    """A request to place an order on Binance Futures."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal  # in base asset units
    price: Decimal | None  # required for LIMIT; None for MARKET
    stop_price: Decimal | None  # required for STOP orders
    strategy_id: str  # links to the strategy that triggered the trade
    reduce_only: bool = False  # True for closing orders


@dataclass
class FillRecord:
    """Actual execution details after a fill."""

    order_id: str
    symbol: str
    side: OrderSide
    filled_quantity: Decimal
    avg_fill_price: Decimal
    commission: Decimal  # total fee paid in quote asset
    commission_asset: str  # e.g. "USDT"
    fill_time: datetime
    realized_pnl: Decimal  # from Binance (0 for opening trades)


@dataclass
class OrderResult:
    """Full outcome of an order placement attempt."""

    request: OrderRequest
    order_id: str | None  # None if placement failed
    status: OrderStatus
    fill: FillRecord | None  # None if not yet filled or failed
    leverage_used: int  # actual leverage set on the symbol
    estimated_cost_pct: float  # from Decision Engine cost estimate
    actual_cost_pct: float | None  # computed from fill (fee / notional)
    cost_overrun_flagged: bool  # True if actual > 2× estimated (Rule 20)
    error_message: str | None  # set on failure
    approved: bool  # False if pre-flight checks failed
    compliance_recorded: bool  # True only when required post-fill persistence succeeded
