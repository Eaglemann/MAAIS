"""Compliance layer dataclasses — Rule 8 (BEHAVIOURS.md).

TradeRecordData: all 11 mandatory fields for a completed trade.
LotData:         an open FIFO lot (position entry waiting to be closed).
PostTradeReasoningData: decision context stored with every trade.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass
class LotData:
    """An open position lot — the FIFO unit of tracking (Rule 9)."""
    trade_id: str          # UUID assigned at open
    asset: str             # e.g. "BTCUSDT"
    side: str              # "long" | "short"
    entry_price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    entry_fees: Decimal    # fees paid at open; allocated proportionally on partial closes
    strategy_id: str
    opened_at: datetime


@dataclass
class TradeRecordData:
    """Completed trade — exactly the 11 mandatory fields from Rule 8.

    Created by the FIFO ledger when a lot (or portion of one) is closed.
    """
    # Rule 8 field 1
    trade_id: str
    # Rule 8 field 2
    timestamp: datetime          # close (exit) timestamp
    # Rule 8 field 3
    asset: str
    # Rule 8 field 4
    entry_price: Decimal
    # Rule 8 field 5
    exit_price: Decimal
    # Rule 8 field 6
    position_size: Decimal       # size of the matched/closed portion
    # Rule 8 field 7
    fees: Decimal                # entry fees (allocated) + exit fees combined
    # Rule 8 field 8
    funding_paid_received: Decimal   # negative = paid, positive = received
    # Rule 8 field 9
    profit_loss: Decimal         # realised P&L after fees and funding
    # Rule 8 field 10
    exchange_rate: Decimal       # USDT → local currency at close time
    # Rule 8 field 11
    strategy_id: str

    # Non-Rule-8 metadata (useful context, not compliance-critical)
    position_side: str = "long"  # "long" | "short"
    entry_lot_id: str = ""       # original lot's trade_id (if partial close creates new ID)

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "timestamp": self.timestamp.isoformat(),
            "asset": self.asset,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "position_size": str(self.position_size),
            "fees": str(self.fees),
            "funding_paid_received": str(self.funding_paid_received),
            "profit_loss": str(self.profit_loss),
            "exchange_rate": str(self.exchange_rate),
            "strategy_id": self.strategy_id,
            "position_side": self.position_side,
        }


@dataclass
class PostTradeReasoningData:
    """Decision context attached to every trade (supports recursive learning, Batch 9).

    Stores the raw agent outputs and final EV calculation so the learning engine
    can evaluate which agents were correct.
    """
    trade_id: str
    agent_outputs: dict = field(default_factory=dict)
    # Format: {agent_name: {hypothesis, probability, confidence, risk_estimate}}
    consensus: dict = field(default_factory=dict)
    # Format: {ev: float, probability_consensus: float, confidence_weighted: float}
    reasoning_summary: str = ""
    created_at: datetime = field(default_factory=lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ))
