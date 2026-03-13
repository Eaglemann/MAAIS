"""Dataclasses for market data transfer between components.

These are lightweight in-memory representations used by connectors,
validators, aggregators, and Kafka producers. SQLAlchemy ORM models
(models.py) are used only for database persistence.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class KlineData:
    symbol: str
    timeframe: str           # "1m", "5m", "15m", "1h"
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal          # base asset volume
    close_time: datetime
    quote_volume: Decimal    # quote asset volume
    trade_count: int
    taker_buy_volume: Decimal
    taker_buy_quote_volume: Decimal
    is_closed: bool = True   # False = candle still forming (WebSocket live update)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
            "close_time": self.close_time.isoformat(),
            "quote_volume": str(self.quote_volume),
            "trade_count": self.trade_count,
            "taker_buy_volume": str(self.taker_buy_volume),
            "taker_buy_quote_volume": str(self.taker_buy_quote_volume),
            "is_closed": self.is_closed,
        }


@dataclass
class FundingRateData:
    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "funding_time": self.funding_time.isoformat(),
            "funding_rate": str(self.funding_rate),
            "mark_price": str(self.mark_price),
        }


@dataclass
class OrderBookSnapshot:
    symbol: str
    timestamp: datetime
    bids: list[tuple[Decimal, Decimal]]   # (price, quantity) sorted desc by price
    asks: list[tuple[Decimal, Decimal]]   # (price, quantity) sorted asc by price
    last_update_id: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "bids": [[str(p), str(q)] for p, q in self.bids],
            "asks": [[str(p), str(q)] for p, q in self.asks],
            "last_update_id": self.last_update_id,
        }


@dataclass
class TradeData:
    symbol: str
    timestamp: datetime
    price: Decimal
    quantity: Decimal
    is_buyer_maker: bool   # True = sell-side aggressor
    trade_id: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "price": str(self.price),
            "quantity": str(self.quantity),
            "is_buyer_maker": self.is_buyer_maker,
            "trade_id": self.trade_id,
        }
