"""FIFO position ledger — Rule 9 (BEHAVIOURS.md).

Tracks open positions as an ordered queue of lots per (asset, side).
When closing, oldest lots are matched first (First In, First Out).

Terminology:
  lot     — a single open position entry (asset + side + entry_price + size)
  close   — consuming one or more lots to realise P&L

P&L formula per matched portion:
  long:  P&L = (exit_price − entry_price) × size − total_fees + funding
  short: P&L = (entry_price − exit_price) × size − total_fees + funding

This class is stateful in-memory.  Call load_open_lots() on startup to
restore state from the database (via TradeLogger).
"""

import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal

from maais.compliance.schemas import LotData, TradeRecordData
from maais.core.logging import get_logger

logger = get_logger(__name__)

_ZERO = Decimal("0")


def _new_id() -> str:
    return str(uuid.uuid4())


def _pnl(
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    size: Decimal,
    fees: Decimal,
    funding: Decimal,
) -> Decimal:
    if side == "long":
        gross = (exit_price - entry_price) * size
    else:
        gross = (entry_price - exit_price) * size
    return gross - fees + funding


class FifoLedger:
    """In-memory FIFO ledger.  Thread-unsafe — use within a single async task."""

    def __init__(self) -> None:
        # Key: (asset, side)  →  deque of LotData (oldest first)
        self._lots: dict[tuple[str, str], deque[LotData]] = defaultdict(deque)

    # ── State recovery ────────────────────────────────────────────────────────

    def load_lots(self, lots: list[LotData]) -> None:
        """Restore in-memory state from persisted lots (called at startup)."""
        for lot in sorted(lots, key=lambda l: l.opened_at):
            self._lots[(lot.asset, lot.side)].append(lot)
        logger.info("fifo_ledger_loaded", total_lots=len(lots))

    # ── Opening a position ────────────────────────────────────────────────────

    def open_lot(
        self,
        asset: str,
        side: str,
        size: Decimal,
        entry_price: Decimal,
        entry_fees: Decimal,
        strategy_id: str,
        opened_at: datetime | None = None,
        trade_id: str | None = None,
    ) -> LotData:
        """Register a new open position lot.

        Returns the LotData — caller must persist it via TradeLogger.
        """
        if side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {side!r}")
        if size <= _ZERO:
            raise ValueError(f"size must be positive, got {size}")

        lot = LotData(
            trade_id=trade_id or _new_id(),
            asset=asset,
            side=side,
            entry_price=entry_price,
            original_size=size,
            remaining_size=size,
            entry_fees=entry_fees,
            strategy_id=strategy_id,
            opened_at=opened_at or datetime.now(timezone.utc),
        )
        self._lots[(asset, side)].appendleft(lot)  # newest to left for display
        # Re-sort by opened_at to maintain FIFO order
        self._lots[(asset, side)] = deque(
            sorted(self._lots[(asset, side)], key=lambda l: l.opened_at)
        )
        logger.info(
            "lot_opened",
            trade_id=lot.trade_id,
            asset=asset,
            side=side,
            size=str(size),
            entry_price=str(entry_price),
        )
        return lot

    # ── Closing a position ────────────────────────────────────────────────────

    def close_position(
        self,
        asset: str,
        side: str,
        close_size: Decimal,
        exit_price: Decimal,
        exit_fees: Decimal,        # total fees for this entire close order
        funding_paid_received: Decimal,
        exchange_rate: Decimal,
        closed_at: datetime | None = None,
    ) -> list[TradeRecordData]:
        """Match close_size against oldest lots (FIFO).  Returns completed TradeRecords.

        A single close call may produce multiple TradeRecords if it spans lots.
        Caller must persist all returned records via TradeLogger.
        """
        key = (asset, side)
        queue = self._lots.get(key)
        if not queue:
            raise ValueError(f"No open {side} lots for {asset}")

        available = sum(lot.remaining_size for lot in queue)
        if close_size > available:
            raise ValueError(
                f"Cannot close {close_size} — only {available} available for {asset} {side}"
            )

        timestamp = closed_at or datetime.now(timezone.utc)
        records: list[TradeRecordData] = []
        remaining_close = close_size
        # Distribute exit fees proportionally across matched lots
        total_exit_fees = exit_fees
        # Distribute funding proportionally across matched lots
        total_funding = funding_paid_received

        while remaining_close > _ZERO and queue:
            lot = queue[0]
            matched = min(lot.remaining_size, remaining_close)
            match_ratio = matched / close_size

            # Allocate fees and funding proportionally to this match
            allocated_exit_fees = total_exit_fees * match_ratio
            allocated_funding = total_funding * match_ratio
            allocated_entry_fees = lot.entry_fees * (matched / lot.original_size)
            total_fees_for_match = allocated_entry_fees + allocated_exit_fees

            pnl = _pnl(side, lot.entry_price, exit_price, matched, total_fees_for_match, allocated_funding)

            record = TradeRecordData(
                trade_id=_new_id(),
                timestamp=timestamp,
                asset=asset,
                entry_price=lot.entry_price,
                exit_price=exit_price,
                position_size=matched,
                fees=total_fees_for_match,
                funding_paid_received=allocated_funding,
                profit_loss=pnl,
                exchange_rate=exchange_rate,
                strategy_id=lot.strategy_id,
                position_side=side,
                entry_lot_id=lot.trade_id,
            )
            records.append(record)

            lot.remaining_size -= matched
            remaining_close -= matched

            if lot.remaining_size == _ZERO:
                queue.popleft()
                logger.debug("lot_fully_closed", lot_id=lot.trade_id)
            else:
                logger.debug("lot_partially_closed", lot_id=lot.trade_id, remaining=str(lot.remaining_size))

        logger.info(
            "position_closed",
            asset=asset,
            side=side,
            close_size=str(close_size),
            exit_price=str(exit_price),
            records_created=len(records),
            total_pnl=str(sum(r.profit_loss for r in records)),
        )
        return records

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_open_size(self, asset: str, side: str) -> Decimal:
        return sum(
            (lot.remaining_size for lot in self._lots.get((asset, side), [])),
            _ZERO,
        )

    def get_open_lots(self, asset: str, side: str) -> list[LotData]:
        return list(self._lots.get((asset, side), []))

    def all_open_positions(self) -> dict[tuple[str, str], Decimal]:
        return {
            key: sum((lot.remaining_size for lot in lots), _ZERO)
            for key, lots in self._lots.items()
            if any(lot.remaining_size > _ZERO for lot in lots)
        }
