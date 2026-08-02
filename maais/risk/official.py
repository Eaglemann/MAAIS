from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from maais.domain.enums import Direction, QualityStatus
from maais.domain.json import JsonValue, content_hash, freeze_json


class RiskCheck(StrEnum):
    KELLY = "kelly"
    PRICE_AND_STOP = "price_and_stop"
    DRAWDOWN = "drawdown"
    CORRELATION = "correlation"
    TRADE_RISK_AT_STOP = "trade_risk_at_stop"
    PORTFOLIO_LOSS_AT_STOP = "portfolio_loss_at_stop"
    GROSS_NOTIONAL = "gross_notional"
    MARGIN = "margin"


@dataclass(frozen=True, slots=True)
class OfficialRiskPolicy:
    maximum_kelly_fraction: Decimal
    maximum_trade_loss_fraction: Decimal
    maximum_portfolio_loss_fraction: Decimal
    maximum_gross_notional_fraction: Decimal
    maximum_margin_fraction: Decimal
    minimum_aligned_correlation_returns: int
    correlation_reduce_20_threshold: Decimal
    correlation_reduce_40_threshold: Decimal
    correlation_block_threshold: Decimal
    drawdown_reduce_25_threshold: Decimal
    drawdown_reduce_50_threshold: Decimal
    drawdown_halt_threshold: Decimal

    def __post_init__(self) -> None:
        for name in (
            "maximum_kelly_fraction",
            "maximum_trade_loss_fraction",
            "maximum_portfolio_loss_fraction",
            "maximum_gross_notional_fraction",
            "maximum_margin_fraction",
            "correlation_reduce_20_threshold",
            "correlation_reduce_40_threshold",
            "correlation_block_threshold",
            "drawdown_reduce_25_threshold",
            "drawdown_reduce_50_threshold",
            "drawdown_halt_threshold",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.minimum_aligned_correlation_returns < 2:
            raise ValueError("correlation warmup must require at least two returns")
        if not (
            self.correlation_reduce_20_threshold
            < self.correlation_reduce_40_threshold
            < self.correlation_block_threshold
            <= 1
        ):
            raise ValueError("correlation thresholds must be ordered within [0, 1]")
        if not (
            self.drawdown_reduce_25_threshold
            < self.drawdown_reduce_50_threshold
            < self.drawdown_halt_threshold
            < 1
        ):
            raise ValueError("drawdown thresholds must be ordered within [0, 1]")

    @classmethod
    def conservative(cls) -> OfficialRiskPolicy:
        return cls(
            maximum_kelly_fraction=Decimal("0.25"),
            maximum_trade_loss_fraction=Decimal("0.02"),
            maximum_portfolio_loss_fraction=Decimal("0.06"),
            maximum_gross_notional_fraction=Decimal("1"),
            maximum_margin_fraction=Decimal("1"),
            minimum_aligned_correlation_returns=60,
            correlation_reduce_20_threshold=Decimal("0.30"),
            correlation_reduce_40_threshold=Decimal("0.60"),
            correlation_block_threshold=Decimal("0.70"),
            drawdown_reduce_25_threshold=Decimal("0.05"),
            drawdown_reduce_50_threshold=Decimal("0.10"),
            drawdown_halt_threshold=Decimal("0.20"),
        )


@dataclass(frozen=True, slots=True)
class DrawdownSnapshot:
    peak_equity: Decimal
    current_equity: Decimal

    def __post_init__(self) -> None:
        if (
            not self.peak_equity.is_finite()
            or not self.current_equity.is_finite()
            or self.peak_equity <= 0
            or self.current_equity < 0
            or self.current_equity > self.peak_equity
        ):
            raise ValueError("drawdown equity inputs are invalid")

    @property
    def drawdown_fraction(self) -> Decimal:
        return (self.peak_equity - self.current_equity) / self.peak_equity

    def to_dict(self) -> dict[str, str]:
        return {
            "peak_equity": str(self.peak_equity),
            "current_equity": str(self.current_equity),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DrawdownSnapshot:
        return cls(Decimal(str(value["peak_equity"])), Decimal(str(value["current_equity"])))


@dataclass(frozen=True, slots=True)
class OpenRiskPosition:
    symbol: str
    notional: Decimal
    loss_at_stop: Decimal
    margin: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("open risk position symbol must be uppercase")
        for name in ("notional", "loss_at_stop", "margin"):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"open position {name} must be nonnegative and finite")


@dataclass(frozen=True, slots=True)
class CorrelationObservation:
    other_symbol: str
    aligned_return_count: int
    correlation: Decimal | None

    def __post_init__(self) -> None:
        if not self.other_symbol or self.other_symbol != self.other_symbol.upper():
            raise ValueError("correlation symbol must be uppercase")
        if self.aligned_return_count < 0:
            raise ValueError("aligned return count cannot be negative")
        if self.correlation is not None and (
            not self.correlation.is_finite()
            or self.correlation < Decimal("-1")
            or self.correlation > Decimal("1")
        ):
            raise ValueError("correlation must be finite and within [-1, 1]")


@dataclass(frozen=True, slots=True)
class OfficialRiskRequest:
    symbol: str
    direction: Direction
    capital: Decimal
    executable_price: Decimal
    stop_price: Decimal
    p_win: Decimal
    expected_gain_fraction: Decimal
    expected_loss_fraction: Decimal
    leverage: int
    drawdown: DrawdownSnapshot
    open_positions: tuple[OpenRiskPosition, ...]
    correlations: tuple[CorrelationObservation, ...]
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("risk request symbol must be uppercase")
        if self.direction is Direction.NEUTRAL:
            raise ValueError("risk request direction must be long or short")
        for name in (
            "capital",
            "executable_price",
            "stop_price",
            "expected_gain_fraction",
            "expected_loss_fraction",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if not self.p_win.is_finite() or self.p_win < 0 or self.p_win > 1:
            raise ValueError("p_win must be a finite Decimal in [0, 1]")
        if self.leverage <= 0:
            raise ValueError("leverage must be positive")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() != timedelta(0):
            raise ValueError("risk evaluation time must be UTC-aware")
        position_names = tuple(item.symbol for item in self.open_positions)
        correlation_names = tuple(item.other_symbol for item in self.correlations)
        if len(set(position_names)) != len(position_names):
            raise ValueError("open risk positions must be unique by symbol")
        if len(set(correlation_names)) != len(correlation_names):
            raise ValueError("correlation observations must be unique by symbol")
        allowed_correlation_names = set(position_names) - {self.symbol}
        if not set(correlation_names) <= allowed_correlation_names:
            raise ValueError("correlation observations must belong to open portfolio symbols")
        object.__setattr__(
            self,
            "open_positions",
            tuple(sorted(self.open_positions, key=lambda item: item.symbol)),
        )
        object.__setattr__(
            self,
            "correlations",
            tuple(sorted(self.correlations, key=lambda item: item.other_symbol)),
        )


@dataclass(frozen=True, slots=True)
class RiskGateResult:
    check: RiskCheck
    status: QualityStatus
    reason_code: str
    details: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("risk gate reason code is required")
        normalized = freeze_json(self.details)
        if not isinstance(normalized, Mapping):
            raise TypeError("risk gate details must be an object")
        object.__setattr__(self, "details", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "status": self.status,
            "reason_code": self.reason_code,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class OfficialRiskDecision:
    approved: bool
    quantity: Decimal
    notional: Decimal
    risk_at_stop: Decimal
    margin: Decimal
    half_kelly_fraction: Decimal
    drawdown_multiplier: Decimal
    correlation_multiplier: Decimal
    input_snapshot: Mapping[str, JsonValue]
    gates: tuple[RiskGateResult, ...]
    rejection_reason: str | None
    content_hash: str

    def __post_init__(self) -> None:
        if tuple(item.check for item in self.gates) != tuple(RiskCheck):
            raise ValueError("official risk decision must contain every ordered risk gate")
        for name in (
            "quantity",
            "notional",
            "risk_at_stop",
            "margin",
            "half_kelly_fraction",
            "drawdown_multiplier",
            "correlation_multiplier",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"risk decision {name} must be nonnegative and finite")
        if self.approved != all(item.status is QualityStatus.PASSED for item in self.gates):
            raise ValueError("risk approval must match its complete gate chain")
        if self.approved == (self.rejection_reason is not None):
            raise ValueError("risk rejection reason does not match approval")
        if len(self.content_hash) != 64:
            raise ValueError("risk decision content hash must be SHA-256")
        normalized = freeze_json(self.input_snapshot)
        if not isinstance(normalized, Mapping):
            raise TypeError("risk decision input snapshot must be an object")
        object.__setattr__(self, "input_snapshot", normalized)


def _gate(
    check: RiskCheck,
    status: QualityStatus,
    reason_code: str,
    **details: object,
) -> RiskGateResult:
    normalized = freeze_json(details)
    if not isinstance(normalized, Mapping):
        raise TypeError("risk gate details must be an object")
    return RiskGateResult(check, status, reason_code, normalized)


class OfficialRiskEngine:
    def __init__(self, policy: OfficialRiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, request: OfficialRiskRequest) -> OfficialRiskDecision:
        input_snapshot = self._input_snapshot(request)
        gain_loss_ratio = request.expected_gain_fraction / request.expected_loss_fraction
        full_kelly = (
            request.p_win * gain_loss_ratio - (Decimal("1") - request.p_win)
        ) / gain_loss_ratio
        half_kelly = min(full_kelly / Decimal("2"), self._policy.maximum_kelly_fraction)
        if half_kelly <= 0:
            return self._early_rejection(
                input_snapshot=input_snapshot,
                half_kelly=max(half_kelly, Decimal("0")),
                completed=(
                    _gate(
                        RiskCheck.KELLY,
                        QualityStatus.FAILED,
                        "kelly_nonpositive",
                        p_win=request.p_win,
                        gain_loss_ratio=gain_loss_ratio,
                        full_kelly=full_kelly,
                    ),
                ),
            )
        gates: list[RiskGateResult] = [
            _gate(
                RiskCheck.KELLY,
                QualityStatus.PASSED,
                "kelly_positive",
                p_win=request.p_win,
                gain_loss_ratio=gain_loss_ratio,
                half_kelly=half_kelly,
            )
        ]

        stop_is_valid = (
            request.direction is Direction.LONG and request.stop_price < request.executable_price
        ) or (
            request.direction is Direction.SHORT and request.stop_price > request.executable_price
        )
        if not stop_is_valid:
            gates.append(
                _gate(
                    RiskCheck.PRICE_AND_STOP,
                    QualityStatus.FAILED,
                    "stop_not_protective",
                    direction=request.direction,
                    executable_price=request.executable_price,
                    stop_price=request.stop_price,
                )
            )
            return self._early_rejection(
                input_snapshot=input_snapshot,
                half_kelly=half_kelly,
                completed=tuple(gates),
            )
        stop_distance = abs(request.executable_price - request.stop_price)
        gates.append(
            _gate(
                RiskCheck.PRICE_AND_STOP,
                QualityStatus.PASSED,
                "executable_price_and_stop_valid",
                executable_price=request.executable_price,
                stop_price=request.stop_price,
                stop_distance=stop_distance,
            )
        )

        drawdown = request.drawdown.drawdown_fraction
        if drawdown >= self._policy.drawdown_halt_threshold:
            gates.append(
                _gate(
                    RiskCheck.DRAWDOWN,
                    QualityStatus.FAILED,
                    "drawdown_halt",
                    drawdown_fraction=drawdown,
                    peak_equity=request.drawdown.peak_equity,
                    current_equity=request.drawdown.current_equity,
                )
            )
            return self._early_rejection(
                input_snapshot=input_snapshot,
                half_kelly=half_kelly,
                completed=tuple(gates),
            )
        if drawdown >= self._policy.drawdown_reduce_50_threshold:
            drawdown_multiplier = Decimal("0.5")
        elif drawdown >= self._policy.drawdown_reduce_25_threshold:
            drawdown_multiplier = Decimal("0.75")
        else:
            drawdown_multiplier = Decimal("1")
        gates.append(
            _gate(
                RiskCheck.DRAWDOWN,
                QualityStatus.PASSED,
                "drawdown_within_limit",
                drawdown_fraction=drawdown,
                multiplier=drawdown_multiplier,
                peak_equity=request.drawdown.peak_equity,
                current_equity=request.drawdown.current_equity,
            )
        )

        correlation_multiplier, correlation_gate = self._correlation_gate(request)
        gates.append(correlation_gate)
        if correlation_gate.status is not QualityStatus.PASSED:
            return self._early_rejection(
                input_snapshot=input_snapshot,
                half_kelly=half_kelly,
                completed=tuple(gates),
                drawdown_multiplier=drawdown_multiplier,
            )

        risk_fraction = min(half_kelly, self._policy.maximum_trade_loss_fraction)
        risk_budget = request.capital * risk_fraction * drawdown_multiplier * correlation_multiplier
        quantity = risk_budget / stop_distance
        notional = quantity * request.executable_price
        risk_at_stop = quantity * stop_distance
        margin = notional / Decimal(request.leverage)
        gates.append(
            _gate(
                RiskCheck.TRADE_RISK_AT_STOP,
                QualityStatus.PASSED,
                "trade_risk_sized_from_stop",
                risk_fraction=risk_fraction,
                risk_budget=risk_budget,
                quantity=quantity,
                risk_at_stop=risk_at_stop,
            )
        )

        existing_loss = sum(
            (item.loss_at_stop for item in request.open_positions), start=Decimal("0")
        )
        total_loss = existing_loss + risk_at_stop
        loss_limit = request.capital * self._policy.maximum_portfolio_loss_fraction
        gates.append(
            _gate(
                RiskCheck.PORTFOLIO_LOSS_AT_STOP,
                QualityStatus.PASSED if total_loss <= loss_limit else QualityStatus.FAILED,
                "portfolio_loss_at_stop_within_limit"
                if total_loss <= loss_limit
                else "portfolio_loss_at_stop_exceeded",
                existing_loss_at_stop=existing_loss,
                proposed_loss_at_stop=risk_at_stop,
                total_loss_at_stop=total_loss,
                limit=loss_limit,
            )
        )

        existing_notional = sum(
            (item.notional for item in request.open_positions), start=Decimal("0")
        )
        total_notional = existing_notional + notional
        notional_limit = request.capital * self._policy.maximum_gross_notional_fraction
        gates.append(
            _gate(
                RiskCheck.GROSS_NOTIONAL,
                QualityStatus.PASSED if total_notional <= notional_limit else QualityStatus.FAILED,
                "gross_notional_within_limit"
                if total_notional <= notional_limit
                else "gross_notional_exceeded",
                existing_notional=existing_notional,
                proposed_notional=notional,
                total_notional=total_notional,
                limit=notional_limit,
            )
        )

        existing_margin = sum((item.margin for item in request.open_positions), start=Decimal("0"))
        total_margin = existing_margin + margin
        margin_limit = request.capital * self._policy.maximum_margin_fraction
        gates.append(
            _gate(
                RiskCheck.MARGIN,
                QualityStatus.PASSED if total_margin <= margin_limit else QualityStatus.FAILED,
                "margin_within_limit" if total_margin <= margin_limit else "margin_exceeded",
                existing_margin=existing_margin,
                proposed_margin=margin,
                total_margin=total_margin,
                limit=margin_limit,
            )
        )
        return self._decision(
            input_snapshot=input_snapshot,
            quantity=quantity,
            notional=notional,
            risk_at_stop=risk_at_stop,
            margin=margin,
            half_kelly=half_kelly,
            drawdown_multiplier=drawdown_multiplier,
            correlation_multiplier=correlation_multiplier,
            gates=tuple(gates),
        )

    def _correlation_gate(self, request: OfficialRiskRequest) -> tuple[Decimal, RiskGateResult]:
        other_symbols = tuple(
            item.symbol for item in request.open_positions if item.symbol != request.symbol
        )
        if not other_symbols:
            return Decimal("1"), _gate(
                RiskCheck.CORRELATION,
                QualityStatus.PASSED,
                "single_symbol_or_empty_portfolio",
                multiplier=Decimal("1"),
            )
        observations = {item.other_symbol: item for item in request.correlations}
        missing = sorted(set(other_symbols) - observations.keys())
        if missing:
            return Decimal("0"), _gate(
                RiskCheck.CORRELATION,
                QualityStatus.NOT_APPLICABLE,
                "correlation_observation_missing",
                symbols=missing,
            )
        cold = sorted(
            symbol
            for symbol in other_symbols
            if observations[symbol].aligned_return_count
            < self._policy.minimum_aligned_correlation_returns
        )
        if cold:
            return Decimal("0"), _gate(
                RiskCheck.CORRELATION,
                QualityStatus.NOT_APPLICABLE,
                "correlation_history_cold",
                symbols=cold,
                minimum_returns=self._policy.minimum_aligned_correlation_returns,
            )
        unavailable = sorted(
            symbol for symbol in other_symbols if observations[symbol].correlation is None
        )
        if unavailable:
            return Decimal("0"), _gate(
                RiskCheck.CORRELATION,
                QualityStatus.NOT_APPLICABLE,
                "correlation_unavailable",
                symbols=unavailable,
            )
        multiplier = Decimal("1")
        values: dict[str, Decimal] = {}
        for symbol in other_symbols:
            raw = observations[symbol].correlation
            if raw is None:
                raise RuntimeError("warm correlation unexpectedly has no value")
            value = abs(raw)
            values[symbol] = value
            if value >= self._policy.correlation_block_threshold:
                return Decimal("0"), _gate(
                    RiskCheck.CORRELATION,
                    QualityStatus.FAILED,
                    "correlation_cluster_blocked",
                    correlations=values,
                    blocking_symbol=symbol,
                )
            if value >= self._policy.correlation_reduce_40_threshold:
                multiplier = min(multiplier, Decimal("0.6"))
            elif value >= self._policy.correlation_reduce_20_threshold:
                multiplier = min(multiplier, Decimal("0.8"))
        return multiplier, _gate(
            RiskCheck.CORRELATION,
            QualityStatus.PASSED,
            "correlation_warm_and_within_limit",
            correlations=values,
            multiplier=multiplier,
            aligned_returns={
                symbol: observations[symbol].aligned_return_count for symbol in other_symbols
            },
        )

    def _early_rejection(
        self,
        *,
        input_snapshot: Mapping[str, JsonValue],
        half_kelly: Decimal,
        completed: tuple[RiskGateResult, ...],
        drawdown_multiplier: Decimal = Decimal("0"),
    ) -> OfficialRiskDecision:
        gates = list(completed)
        for check in tuple(RiskCheck)[len(gates) :]:
            gates.append(
                _gate(
                    check,
                    QualityStatus.NOT_APPLICABLE,
                    "prior_risk_gate_failed",
                    blocking_check=completed[-1].check,
                )
            )
        return self._decision(
            input_snapshot=input_snapshot,
            quantity=Decimal("0"),
            notional=Decimal("0"),
            risk_at_stop=Decimal("0"),
            margin=Decimal("0"),
            half_kelly=half_kelly,
            drawdown_multiplier=drawdown_multiplier,
            correlation_multiplier=Decimal("0"),
            gates=tuple(gates),
        )

    @staticmethod
    def _decision(
        *,
        input_snapshot: Mapping[str, JsonValue],
        quantity: Decimal,
        notional: Decimal,
        risk_at_stop: Decimal,
        margin: Decimal,
        half_kelly: Decimal,
        drawdown_multiplier: Decimal,
        correlation_multiplier: Decimal,
        gates: tuple[RiskGateResult, ...],
    ) -> OfficialRiskDecision:
        approved = all(item.status is QualityStatus.PASSED for item in gates)
        rejection = next(
            (item.reason_code for item in gates if item.status is not QualityStatus.PASSED),
            None,
        )
        normalized = {
            "approved": approved,
            "quantity": quantity,
            "notional": notional,
            "risk_at_stop": risk_at_stop,
            "margin": margin,
            "half_kelly_fraction": half_kelly,
            "drawdown_multiplier": drawdown_multiplier,
            "correlation_multiplier": correlation_multiplier,
            "input_snapshot": input_snapshot,
            "gates": [item.to_dict() for item in gates],
            "rejection_reason": rejection,
        }
        return OfficialRiskDecision(
            approved=approved,
            quantity=quantity,
            notional=notional,
            risk_at_stop=risk_at_stop,
            margin=margin,
            half_kelly_fraction=half_kelly,
            drawdown_multiplier=drawdown_multiplier,
            correlation_multiplier=correlation_multiplier,
            input_snapshot=input_snapshot,
            gates=gates,
            rejection_reason=rejection,
            content_hash=content_hash(normalized),
        )

    def _input_snapshot(self, request: OfficialRiskRequest) -> Mapping[str, JsonValue]:
        normalized = freeze_json(
            {
                "request": {
                    "symbol": request.symbol,
                    "direction": request.direction,
                    "capital": request.capital,
                    "executable_price": request.executable_price,
                    "stop_price": request.stop_price,
                    "p_win": request.p_win,
                    "expected_gain_fraction": request.expected_gain_fraction,
                    "expected_loss_fraction": request.expected_loss_fraction,
                    "leverage": request.leverage,
                    "drawdown": request.drawdown.to_dict(),
                    "open_positions": [
                        {
                            "symbol": item.symbol,
                            "notional": item.notional,
                            "loss_at_stop": item.loss_at_stop,
                            "margin": item.margin,
                        }
                        for item in request.open_positions
                    ],
                    "correlations": [
                        {
                            "other_symbol": item.other_symbol,
                            "aligned_return_count": item.aligned_return_count,
                            "correlation": item.correlation,
                        }
                        for item in request.correlations
                    ],
                    "evaluated_at": request.evaluated_at,
                },
                "policy": {
                    "maximum_kelly_fraction": self._policy.maximum_kelly_fraction,
                    "maximum_trade_loss_fraction": self._policy.maximum_trade_loss_fraction,
                    "maximum_portfolio_loss_fraction": (
                        self._policy.maximum_portfolio_loss_fraction
                    ),
                    "maximum_gross_notional_fraction": (
                        self._policy.maximum_gross_notional_fraction
                    ),
                    "maximum_margin_fraction": self._policy.maximum_margin_fraction,
                    "minimum_aligned_correlation_returns": (
                        self._policy.minimum_aligned_correlation_returns
                    ),
                    "correlation_reduce_20_threshold": (
                        self._policy.correlation_reduce_20_threshold
                    ),
                    "correlation_reduce_40_threshold": (
                        self._policy.correlation_reduce_40_threshold
                    ),
                    "correlation_block_threshold": self._policy.correlation_block_threshold,
                    "drawdown_reduce_25_threshold": (self._policy.drawdown_reduce_25_threshold),
                    "drawdown_reduce_50_threshold": (self._policy.drawdown_reduce_50_threshold),
                    "drawdown_halt_threshold": self._policy.drawdown_halt_threshold,
                },
            }
        )
        if not isinstance(normalized, Mapping):
            raise TypeError("risk input snapshot must be an object")
        return normalized
