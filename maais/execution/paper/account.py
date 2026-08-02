from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from maais.domain.enums import Direction, PaperOrderSide, PositionEffect
from maais.execution.paper.clock import require_utc
from maais.execution.paper.market import require_positive_decimal
from maais.execution.paper.positions import PositionState


class InsufficientMargin(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    residuals: tuple[Decimal, Decimal, Decimal]

    @property
    def ok(self) -> bool:
        return all(residual == 0 for residual in self.residuals)


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash_balance: Decimal
    equity: Decimal
    used_margin: Decimal
    free_margin: Decimal
    gross_notional: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    peak_equity: Decimal
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class AccountState:
    experiment_id: UUID
    initial_capital: Decimal
    currency: str
    leverage: int
    cash_balance: Decimal
    peak_equity: Decimal
    positions: Mapping[str, PositionState]
    version: int
    updated_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))

    @classmethod
    def create(
        cls,
        experiment_id: UUID,
        initial_capital: Decimal,
        currency: str,
        *,
        leverage: int = 1,
    ) -> AccountState:
        require_positive_decimal(initial_capital, "initial_capital")
        if not currency:
            raise ValueError("currency is required")
        if not 1 <= leverage <= 5:
            raise ValueError("leverage must be between 1 and 5")
        return cls(
            experiment_id=experiment_id,
            initial_capital=initial_capital,
            currency=currency,
            leverage=leverage,
            cash_balance=initial_capital,
            peak_equity=initial_capital,
            positions={},
            version=0,
            updated_at=None,
        )

    def position(self, symbol: str) -> PositionState:
        try:
            return self.positions[symbol]
        except KeyError as exc:
            raise LookupError(f"no position for {symbol}") from exc

    @property
    def realized_pnl(self) -> Decimal:
        return sum(
            (position.realized_pnl for position in self.positions.values()),
            start=Decimal("0"),
        )

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum(
            (position.unrealized_pnl for position in self.positions.values()),
            start=Decimal("0"),
        )

    @property
    def fees(self) -> Decimal:
        return sum(
            (position.fees for position in self.positions.values()),
            start=Decimal("0"),
        )

    @property
    def funding(self) -> Decimal:
        return sum(
            (position.funding for position in self.positions.values()),
            start=Decimal("0"),
        )

    @property
    def gross_notional(self) -> Decimal:
        return sum(
            (position.gross_notional for position in self.positions.values()),
            start=Decimal("0"),
        )

    @property
    def used_margin(self) -> Decimal:
        return self.gross_notional / Decimal(self.leverage)

    @property
    def equity(self) -> Decimal:
        return self.cash_balance + self.unrealized_pnl

    @property
    def free_margin(self) -> Decimal:
        return self.equity - self.used_margin

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal("0")
        return max(Decimal("0"), (self.peak_equity - self.equity) / self.peak_equity)

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            cash_balance=self.cash_balance,
            equity=self.equity,
            used_margin=self.used_margin,
            free_margin=self.free_margin,
            gross_notional=self.gross_notional,
            unrealized_pnl=self.unrealized_pnl,
            realized_pnl=self.realized_pnl,
            fees=self.fees,
            funding=self.funding,
            peak_equity=self.peak_equity,
            drawdown=self.drawdown,
        )

    def apply_fill(
        self,
        *,
        fill_id: UUID,
        position_id: UUID,
        symbol: str,
        side: PaperOrderSide,
        position_effect: PositionEffect,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fill_at: datetime,
    ) -> AccountState:
        require_positive_decimal(quantity, "quantity")
        require_positive_decimal(price, "price")
        if not isinstance(fee, Decimal) or not fee.is_finite() or fee < 0:
            raise ValueError("fee must be a nonnegative finite Decimal")
        require_utc(fill_at, "fill_at")
        positions = dict(self.positions)
        current = positions.get(symbol)

        if position_effect is PositionEffect.OPEN:
            opening_side = Direction.LONG if side is PaperOrderSide.BUY else Direction.SHORT
            if current is None:
                current = PositionState.empty(position_id, self.experiment_id, symbol)
            elif not current.is_flat and current.position_id != position_id:
                raise ValueError("position_id does not match the open position")
            candidate_position = current.open_fill(
                fill_id=fill_id,
                side=opening_side,
                quantity=quantity,
                price=price,
                fee=fee,
                fill_at=fill_at,
            )
            cash_balance = self.cash_balance - fee
        else:
            if current is None or current.is_flat:
                raise ValueError("reduce-only fill has no open quantity")
            expected_side = (
                PaperOrderSide.SELL if current.side is Direction.LONG else PaperOrderSide.BUY
            )
            if side is not expected_side:
                raise ValueError("reduce-only fill side does not close the position")
            before_realized = current.realized_pnl
            candidate_position = current.close_fill(
                quantity=quantity,
                price=price,
                fee=fee,
                fill_at=fill_at,
            )
            realized_delta = candidate_position.realized_pnl - before_realized
            cash_balance = self.cash_balance + realized_delta - fee

        positions[symbol] = candidate_position
        candidate = replace(
            self,
            cash_balance=cash_balance,
            positions=positions,
            version=self.version + 1,
            updated_at=fill_at,
        )
        candidate = replace(candidate, peak_equity=max(self.peak_equity, candidate.equity))
        if position_effect is PositionEffect.OPEN and candidate.free_margin < 0:
            raise InsufficientMargin("paper account has insufficient free margin")
        if not candidate.reconcile().ok:
            raise ArithmeticError("account reconciliation failed after fill")
        return candidate

    def mark(self, symbol: str, price: Decimal, observed_at: datetime) -> AccountState:
        require_utc(observed_at, "observed_at")
        positions = dict(self.positions)
        positions[symbol] = self.position(symbol).mark(price)
        candidate = replace(
            self,
            positions=positions,
            version=self.version + 1,
            updated_at=observed_at,
        )
        return replace(candidate, peak_equity=max(self.peak_equity, candidate.equity))

    def apply_funding(
        self,
        symbol: str,
        *,
        rate: Decimal,
        observed_at: datetime,
    ) -> AccountState:
        require_utc(observed_at, "observed_at")
        if not isinstance(rate, Decimal) or not rate.is_finite():
            raise ValueError("funding rate must be a finite Decimal")
        position = self.position(symbol)
        if position.is_flat:
            raise ValueError("cannot apply funding to a flat position")
        signed_amount = position.gross_notional * rate
        amount = -signed_amount if position.side is Direction.LONG else signed_amount
        positions = dict(self.positions)
        positions[symbol] = position.apply_funding(amount)
        candidate = replace(
            self,
            cash_balance=self.cash_balance + amount,
            positions=positions,
            version=self.version + 1,
            updated_at=observed_at,
        )
        candidate = replace(candidate, peak_equity=max(self.peak_equity, candidate.equity))
        if not candidate.reconcile().ok:
            raise ArithmeticError("account reconciliation failed after funding")
        return candidate

    def reconcile(self) -> ReconciliationReport:
        expected_cash = self.initial_capital + self.realized_pnl - self.fees + self.funding
        cash_residual = self.cash_balance - expected_cash
        equity_residual = self.equity - (self.cash_balance + self.unrealized_pnl)
        expected_margin = sum(
            (
                position.quantity * position.mark_price / Decimal(self.leverage)
                for position in self.positions.values()
            ),
            start=Decimal("0"),
        )
        margin_residual = self.used_margin - expected_margin
        return ReconciliationReport((cash_residual, equity_residual, margin_residual))
