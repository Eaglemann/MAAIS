"""Trade record writer — assembles the 11-field Rule 8 trade record from a fill.

Integrates with Batch 2 compliance engine:
  - Uses FifoLedger to open/close lots
  - Uses TradeLogger to persist the record
  - Uses ExchangeRateFetcher for the exchange_rate field
  - Uses FundingTracker for funding_paid_received field
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from maais.compliance.exchange_rate import ExchangeRateFetcher
from maais.compliance.fifo_ledger import FifoLedger
from maais.compliance.schemas import TradeRecordData
from maais.compliance.trade_logger import TradeLogger
from maais.core.logging import get_logger
from maais.execution.funding_tracker import FundingTracker
from maais.execution.schemas import FillRecord, OrderSide

logger = get_logger(__name__)


class TradeRecordWriter:
    """Assembles and persists the 11-field Rule 8 trade record on fill."""

    def __init__(
        self,
        ledger: FifoLedger,
        trade_logger: TradeLogger,
        rate_fetcher: ExchangeRateFetcher,
        funding_tracker: FundingTracker,
        strategy_id: str = "default",
    ) -> None:
        self._ledger = ledger
        self._logger = trade_logger
        self._rate_fetcher = rate_fetcher
        self._funding_tracker = funding_tracker
        self._strategy_id = strategy_id

    async def record_open(self, fill: FillRecord, strategy_id: str | None = None) -> None:
        """Record an opening fill — opens a new FIFO lot."""
        sid = strategy_id or self._strategy_id
        # BUY opens a long; SELL opens a short
        side = "long" if fill.side == OrderSide.BUY else "short"
        lot = self._ledger.open_lot(
            asset=fill.symbol,
            side=side,
            size=fill.filled_quantity,
            entry_price=fill.avg_fill_price,
            entry_fees=fill.commission,
            strategy_id=sid,
            opened_at=fill.fill_time,
        )
        await self._logger.save_lot(lot)
        logger.info("lot_opened", symbol=fill.symbol, lot_id=lot.trade_id, side=fill.side.value,
                    qty=str(fill.filled_quantity), price=str(fill.avg_fill_price))

    async def record_close(
        self,
        fill: FillRecord,
        strategy_id: str | None = None,
    ) -> list[TradeRecordData]:
        """Record a closing fill — closes FIFO lots and writes trade records.

        Returns list of TradeRecordData written (one per matched lot).
        """
        exchange_rate = await self._rate_fetcher.get_rate()
        funding = self._funding_tracker.get_cumulative(fill.symbol)

        # Closing side is the opposite of the fill side:
        # a SELL fill closes a long; a BUY fill closes a short
        close_side = "long" if fill.side == OrderSide.SELL else "short"

        records = self._ledger.close_position(
            asset=fill.symbol,
            side=close_side,
            close_size=fill.filled_quantity,
            exit_price=fill.avg_fill_price,
            exit_fees=fill.commission,
            funding_paid_received=funding,
            exchange_rate=exchange_rate,
            closed_at=fill.fill_time,
        )

        await self._logger.save_trade_records(records)
        logger.info("trade_records_written", symbol=fill.symbol, count=len(records))
        return records
