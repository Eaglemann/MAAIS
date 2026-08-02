"""Tests for compliance.fifo_ledger — FIFO position tracking (Rule 9)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from maais.compliance.fifo_ledger import FifoLedger
from maais.compliance.schemas import LotData


def _dt(minute: int = 0) -> datetime:
    return datetime(2024, 1, 1, 0, minute, 0, tzinfo=timezone.utc)


def _ledger() -> FifoLedger:
    return FifoLedger()


class TestOpenLot:
    def test_open_creates_lot(self):
        ledger = _ledger()
        lot = ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("10"), "strat_1"
        )
        assert lot.trade_id
        assert lot.asset == "BTCUSDT"
        assert lot.remaining_size == Decimal("1.0")

    def test_open_size_tracked(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("2.0"), Decimal("50000"), Decimal("10"), "strat_1"
        )
        assert ledger.get_open_size("BTCUSDT", "long") == Decimal("2.0")

    def test_invalid_side_raises(self):
        ledger = _ledger()
        with pytest.raises(ValueError, match="side must be"):
            ledger.open_lot("BTCUSDT", "buy", Decimal("1.0"), Decimal("50000"), Decimal("10"), "s1")

    def test_zero_size_raises(self):
        ledger = _ledger()
        with pytest.raises(ValueError, match="size must be positive"):
            ledger.open_lot("BTCUSDT", "long", Decimal("0"), Decimal("50000"), Decimal("10"), "s1")


class TestClosePosition:
    def test_simple_full_close(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("10"), "s1", _dt(0)
        )
        records = ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("51000"),
            exit_fees=Decimal("10"),
            funding_paid_received=Decimal("0"),
            exchange_rate=Decimal("1.0"),
        )
        assert len(records) == 1
        assert records[0].exit_price == Decimal("51000")
        assert records[0].position_size == Decimal("1.0")

    def test_long_profit_calculation(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("10"), "s1", _dt(0)
        )
        records = ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("51000"),
            exit_fees=Decimal("10"),
            funding_paid_received=Decimal("5"),
            exchange_rate=Decimal("1.0"),
        )
        # gross = (51000 - 50000) * 1 = 1000
        # fees = 10 (entry) + 10 (exit) = 20
        # funding = +5
        # P&L = 1000 - 20 + 5 = 985
        assert records[0].profit_loss == Decimal("985")

    def test_short_profit_calculation(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "short", Decimal("1.0"), Decimal("51000"), Decimal("10"), "s1", _dt(0)
        )
        records = ledger.close_position(
            "BTCUSDT",
            "short",
            Decimal("1.0"),
            Decimal("50000"),
            exit_fees=Decimal("10"),
            funding_paid_received=Decimal("0"),
            exchange_rate=Decimal("1.0"),
        )
        # gross = (51000 - 50000) * 1 = 1000
        # fees = 10 + 10 = 20
        # P&L = 1000 - 20 = 980
        assert records[0].profit_loss == Decimal("980")

    def test_long_loss_calculation(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("10"), "s1", _dt(0)
        )
        records = ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("49000"),
            exit_fees=Decimal("10"),
            funding_paid_received=Decimal("0"),
            exchange_rate=Decimal("1.0"),
        )
        # gross = (49000 - 50000) * 1 = -1000
        # P&L = -1000 - 20 = -1020
        assert records[0].profit_loss == Decimal("-1020")

    def test_partial_close_leaves_remainder(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("2.0"), Decimal("50000"), Decimal("20"), "s1", _dt(0)
        )
        ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("51000"),
            exit_fees=Decimal("5"),
            funding_paid_received=Decimal("0"),
            exchange_rate=Decimal("1.0"),
        )
        assert ledger.get_open_size("BTCUSDT", "long") == Decimal("1.0")

    def test_close_spans_two_lots_fifo_order(self):
        """Oldest lot (lot A, lower entry price) should be matched first."""
        ledger = _ledger()
        # Lot A: opened first (older)
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("40000"), Decimal("5"), "s1", _dt(0)
        )
        # Lot B: opened second (newer)
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("5"), "s1", _dt(1)
        )

        records = ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("2.0"),
            Decimal("55000"),
            exit_fees=Decimal("10"),
            funding_paid_received=Decimal("0"),
            exchange_rate=Decimal("1.0"),
        )
        assert len(records) == 2
        # First record should be from lot A (entry 40000, older)
        assert records[0].entry_price == Decimal("40000")
        # Second record should be from lot B (entry 50000, newer)
        assert records[1].entry_price == Decimal("50000")

    def test_close_size_exceeds_available_raises(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("5"), "s1", _dt(0)
        )
        with pytest.raises(ValueError, match="Cannot close"):
            ledger.close_position(
                "BTCUSDT",
                "long",
                Decimal("2.0"),
                Decimal("51000"),
                exit_fees=Decimal("5"),
                funding_paid_received=Decimal("0"),
                exchange_rate=Decimal("1.0"),
            )

    def test_no_open_lots_raises(self):
        ledger = _ledger()
        with pytest.raises(ValueError, match="No open"):
            ledger.close_position(
                "BTCUSDT",
                "long",
                Decimal("1.0"),
                Decimal("51000"),
                exit_fees=Decimal("5"),
                funding_paid_received=Decimal("0"),
                exchange_rate=Decimal("1.0"),
            )

    def test_full_close_clears_position(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("5"), "s1", _dt(0)
        )
        ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("51000"),
            exit_fees=Decimal("5"),
            funding_paid_received=Decimal("0"),
            exchange_rate=Decimal("1.0"),
        )
        assert ledger.get_open_size("BTCUSDT", "long") == Decimal("0")

    def test_funding_included_in_pnl(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT", "long", Decimal("1.0"), Decimal("50000"), Decimal("0"), "s1", _dt(0)
        )
        records = ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("50000"),  # breakeven on price
            exit_fees=Decimal("0"),
            funding_paid_received=Decimal("100"),
            exchange_rate=Decimal("1.0"),
        )
        # price breakeven + $100 funding received = net +$100
        assert records[0].profit_loss == Decimal("100")


class TestLoadLots:
    def test_load_restores_state(self):
        ledger = _ledger()
        lots = [
            LotData(
                "id1",
                "BTCUSDT",
                "long",
                Decimal("50000"),
                Decimal("1.0"),
                Decimal("1.0"),
                Decimal("5"),
                "s1",
                _dt(0),
            ),
        ]
        ledger.load_lots(lots)
        assert ledger.get_open_size("BTCUSDT", "long") == Decimal("1.0")

    def test_load_preserves_fifo_order(self):
        """Lot opened at minute 0 must be first in queue after reload."""
        ledger = _ledger()
        lots = [
            LotData(
                "id2",
                "BTCUSDT",
                "long",
                Decimal("51000"),
                Decimal("1.0"),
                Decimal("1.0"),
                Decimal("5"),
                "s1",
                _dt(1),
            ),
            LotData(
                "id1",
                "BTCUSDT",
                "long",
                Decimal("50000"),
                Decimal("1.0"),
                Decimal("1.0"),
                Decimal("5"),
                "s1",
                _dt(0),
            ),
        ]
        ledger.load_lots(lots)
        open_lots = ledger.get_open_lots("BTCUSDT", "long")
        assert open_lots[0].trade_id == "id1"  # oldest first


class TestTradeRecord11Fields:
    def test_all_11_fields_present(self):
        ledger = _ledger()
        ledger.open_lot(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("50000"),
            Decimal("5"),
            "strategy_abc",
            _dt(0),
        )
        records = ledger.close_position(
            "BTCUSDT",
            "long",
            Decimal("1.0"),
            Decimal("51000"),
            exit_fees=Decimal("5"),
            funding_paid_received=Decimal("2"),
            exchange_rate=Decimal("0.92"),
        )
        r = records[0]
        # Rule 8: verify all 11 mandatory fields are populated
        assert r.trade_id  # field 1
        assert r.timestamp  # field 2
        assert r.asset  # field 3
        assert r.entry_price > 0  # field 4
        assert r.exit_price > 0  # field 5
        assert r.position_size > 0  # field 6
        assert r.fees >= 0  # field 7
        assert r.funding_paid_received is not None  # field 8
        assert r.profit_loss is not None  # field 9
        assert r.exchange_rate > 0  # field 10
        assert r.strategy_id  # field 11
