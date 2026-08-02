from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from maais.domain.enums import Direction, QualityStatus
from maais.risk.official import (
    CorrelationObservation,
    DrawdownSnapshot,
    OfficialRiskEngine,
    OfficialRiskPolicy,
    OfficialRiskRequest,
    OpenRiskPosition,
    RiskCheck,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)


def _request(**changes) -> OfficialRiskRequest:
    values = {
        "symbol": "BTCUSDT",
        "direction": Direction.LONG,
        "capital": Decimal("10000"),
        "executable_price": Decimal("50000"),
        "stop_price": Decimal("49000"),
        "p_win": Decimal("0.60"),
        "expected_gain_fraction": Decimal("0.02"),
        "expected_loss_fraction": Decimal("0.01"),
        "leverage": 1,
        "drawdown": DrawdownSnapshot(Decimal("10000"), Decimal("10000")),
        "open_positions": (),
        "correlations": (),
        "evaluated_at": NOW,
    }
    values.update(changes)
    return OfficialRiskRequest(**values)


def _gate(decision, check: RiskCheck):
    return next(item for item in decision.gates if item.check is check)


def test_sizes_from_executable_price_and_actual_stop_distance() -> None:
    decision = OfficialRiskEngine(OfficialRiskPolicy.conservative()).evaluate(_request())

    assert decision.approved
    assert decision.quantity == Decimal("0.2")
    assert decision.notional == Decimal("10000.0")
    assert decision.risk_at_stop == Decimal("200.0")
    assert decision.margin == Decimal("10000.0")
    assert {gate.check for gate in decision.gates} == set(RiskCheck)
    assert all(gate.status is QualityStatus.PASSED for gate in decision.gates)


def test_nonpositive_kelly_rejects_before_any_quantity_calculation() -> None:
    decision = OfficialRiskEngine(OfficialRiskPolicy.conservative()).evaluate(
        _request(p_win=Decimal("0.30"))
    )

    assert not decision.approved
    assert decision.quantity == 0
    assert decision.notional == 0
    assert _gate(decision, RiskCheck.KELLY).status is QualityStatus.FAILED
    assert all(
        gate.status is QualityStatus.NOT_APPLICABLE
        for gate in decision.gates
        if gate.check is not RiskCheck.KELLY
    )


def test_multi_symbol_exposure_requires_sixty_aligned_returns() -> None:
    position = OpenRiskPosition(
        symbol="ETHUSDT",
        notional=Decimal("1000"),
        loss_at_stop=Decimal("50"),
        margin=Decimal("500"),
    )
    cold = CorrelationObservation("ETHUSDT", 59, Decimal("0.1"))
    warm = replace(cold, aligned_return_count=60)
    engine = OfficialRiskEngine(OfficialRiskPolicy.conservative())

    rejected = engine.evaluate(
        _request(
            stop_price=Decimal("48750"),
            open_positions=(position,),
            correlations=(cold,),
        )
    )
    admitted = engine.evaluate(
        _request(
            stop_price=Decimal("48750"),
            open_positions=(position,),
            correlations=(warm,),
        )
    )

    assert not rejected.approved
    assert _gate(rejected, RiskCheck.CORRELATION).reason_code == "correlation_history_cold"
    assert admitted.approved
    assert _gate(admitted, RiskCheck.CORRELATION).status is QualityStatus.PASSED


def test_portfolio_loss_at_stop_is_independent_from_gross_and_margin_caps() -> None:
    position = OpenRiskPosition(
        symbol="ETHUSDT",
        notional=Decimal("1000"),
        loss_at_stop=Decimal("500"),
        margin=Decimal("500"),
    )
    correlation = CorrelationObservation("ETHUSDT", 60, Decimal("0.1"))
    decision = OfficialRiskEngine(OfficialRiskPolicy.conservative()).evaluate(
        _request(
            stop_price=Decimal("48750"),
            open_positions=(position,),
            correlations=(correlation,),
        )
    )

    assert not decision.approved
    assert _gate(decision, RiskCheck.PORTFOLIO_LOSS_AT_STOP).status is QualityStatus.FAILED
    assert _gate(decision, RiskCheck.GROSS_NOTIONAL).status is QualityStatus.PASSED
    assert _gate(decision, RiskCheck.MARGIN).status is QualityStatus.PASSED


def test_drawdown_snapshot_is_explicit_restorable_and_reduces_risk() -> None:
    snapshot = DrawdownSnapshot(Decimal("12000"), Decimal("10800"))
    restored = DrawdownSnapshot.from_dict(snapshot.to_dict())
    decision = OfficialRiskEngine(OfficialRiskPolicy.conservative()).evaluate(
        _request(drawdown=restored)
    )

    assert restored == snapshot
    assert restored.drawdown_fraction == Decimal("0.1")
    assert decision.drawdown_multiplier == Decimal("0.5")
    assert decision.risk_at_stop == Decimal("100.00")
    assert (
        decision.content_hash
        == OfficialRiskEngine(OfficialRiskPolicy.conservative())
        .evaluate(_request(drawdown=restored))
        .content_hash
    )


def test_risk_hash_binds_full_request_time_and_policy_snapshot() -> None:
    engine = OfficialRiskEngine(OfficialRiskPolicy.conservative())
    first = engine.evaluate(_request())
    later = engine.evaluate(_request(evaluated_at=NOW.replace(second=1)))

    request_snapshot = first.input_snapshot["request"]
    policy_snapshot = first.input_snapshot["policy"]
    assert request_snapshot["executable_price"] == "50000"  # type: ignore[index]
    assert policy_snapshot["maximum_trade_loss_fraction"] == "0.02"  # type: ignore[index]
    assert first.content_hash != later.content_hash


def test_portfolio_input_permutations_normalize_to_one_risk_hash() -> None:
    positions = (
        OpenRiskPosition("ETHUSDT", Decimal("300"), Decimal("10"), Decimal("300")),
        OpenRiskPosition("SOLUSDT", Decimal("200"), Decimal("10"), Decimal("200")),
    )
    correlations = (
        CorrelationObservation("ETHUSDT", 60, Decimal("0.1")),
        CorrelationObservation("SOLUSDT", 60, Decimal("0.2")),
    )
    engine = OfficialRiskEngine(OfficialRiskPolicy.conservative())

    first = engine.evaluate(
        _request(
            stop_price=Decimal("48750"),
            open_positions=positions,
            correlations=correlations,
        )
    )
    second = engine.evaluate(
        _request(
            stop_price=Decimal("48750"),
            open_positions=tuple(reversed(positions)),
            correlations=tuple(reversed(correlations)),
        )
    )

    assert first.content_hash == second.content_hash
