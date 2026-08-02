from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from maais.config.constants import ALL_AGENTS
from maais.decisions.bundle import (
    AgentEvaluationRecord,
    DecisionBundle,
    DecisionCycleRecord,
    DecisionSummaryRecord,
    GateEvaluationRecord,
    MarketFrameRecord,
    TradeProposalRecord,
)
from maais.domain.enums import (
    DecisionStatus,
    Direction,
    Disposition,
    GateType,
    ProposalStatus,
    QualityStatus,
    ReasonCode,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
EXPERIMENT_ID = UUID("11111111-1111-4111-8111-111111111111")


def _valid_bundle(
    *,
    experiment_id: UUID = EXPERIMENT_ID,
    strategy_version_id: UUID | None = None,
    agent_version_ids: dict[str, UUID] | None = None,
) -> DecisionBundle:
    frame_id = uuid4()
    cycle_id = uuid4()
    market_frame = MarketFrameRecord(
        id=frame_id,
        experiment_id=experiment_id,
        symbol="BTCUSDT",
        venue="binance",
        timeframe="1m",
        bar_open_at=NOW - timedelta(minutes=1),
        bar_close_at=NOW,
        observed_at=NOW + timedelta(milliseconds=100),
        open=Decimal("60000"),
        high=Decimal("60100"),
        low=Decimal("59900"),
        close=Decimal("60050"),
        volume=Decimal("12.5"),
        best_bid=Decimal("60049"),
        best_ask=Decimal("60051"),
        mark_price=Decimal("60050"),
        index_price=Decimal("60049.5"),
        funding_rate=Decimal("0.0001"),
        primary_spot_price=Decimal("60048"),
        secondary_venue_price=Decimal("60047"),
        bar_snapshot={"quote_volume": "750000", "trade_count": 1234},
        orderbook_snapshot={"bids": [["60049", "2"]], "asks": [["60051", "2"]]},
        source_manifest={"closed_bar": {"event_id": "bar-1", "content_hash": "b" * 64}},
        source_sequence={"depth": 42},
        quality_status=QualityStatus.PASSED,
        quality_results={"required_checks": 8, "passed": 8},
        content_hash="a" * 64,
    )
    cycle = DecisionCycleRecord(
        id=cycle_id,
        experiment_id=experiment_id,
        market_frame_id=frame_id,
        strategy_version_id=strategy_version_id or uuid4(),
        symbol="BTCUSDT",
        timeframe="1m",
        cycle_at=NOW,
        regime="trending",
        feature_snapshot={"ema_fast": "60020", "ema_slow": "59980"},
        feature_version="v1",
        status=DecisionStatus.COMPLETED,
        direction=Direction.LONG,
        disposition=Disposition.APPROVED,
        reason_code=ReasonCode.ACCEPTED,
        created_at=NOW + timedelta(milliseconds=110),
        completed_at=NOW + timedelta(milliseconds=240),
    )
    agents = tuple(
        AgentEvaluationRecord(
            id=uuid4(),
            decision_cycle_id=cycle_id,
            agent_version_id=(
                agent_version_ids[name] if agent_version_ids is not None else UUID(int=index + 100)
            ),
            agent_name=name,
            compatible=True,
            enabled=True,
            weight=Decimal("1"),
            direction=Direction.LONG,
            probability=Decimal("0.65"),
            confidence=Decimal("0.70"),
            risk=Decimal("0.20"),
            input_snapshot={"signal": "positive"},
            reason_codes=(ReasonCode.ACCEPTED,),
            explanation={"contribution": "positive"},
            duration_ms=5,
            created_at=NOW + timedelta(milliseconds=150 + index),
        )
        for index, name in enumerate(ALL_AGENTS)
    )
    summary = DecisionSummaryRecord(
        decision_cycle_id=cycle_id,
        consensus_direction=Direction.LONG,
        consensus_probability=Decimal("0.65"),
        consensus_confidence=Decimal("0.70"),
        long_weight=Decimal("8"),
        short_weight=Decimal("0"),
        neutral_weight=Decimal("0"),
        dissenters=(),
        dissent_probability=Decimal("0"),
        dissent_confidence=Decimal("0"),
        challenge_blocked=False,
        expected_gain=Decimal("0.01"),
        expected_loss=Decimal("0.01"),
        gross_ev=Decimal("0.003"),
        funding_carry=Decimal("0"),
        estimated_cost=Decimal("0.001"),
        net_ev=Decimal("0.002"),
        benchmark_return=Decimal("0.0005"),
        alpha_estimate=Decimal("0.0015"),
        consensus_snapshot={"version": 1},
        adversarial_snapshot={"version": 1},
        ev_snapshot={"version": 1},
        cost_snapshot={"version": 1},
    )
    gates = (
        GateEvaluationRecord(
            id=uuid4(),
            decision_cycle_id=cycle_id,
            gate_type=GateType.DATA_QUALITY,
            sequence=1,
            passed=True,
            reason_code=ReasonCode.ACCEPTED,
            input={"quality": "passed"},
            output={"approved": True},
            evaluated_at=NOW + timedelta(milliseconds=200),
            duration_ms=2,
        ),
        GateEvaluationRecord(
            id=uuid4(),
            decision_cycle_id=cycle_id,
            gate_type=GateType.EV,
            sequence=2,
            passed=True,
            reason_code=ReasonCode.ACCEPTED,
            input={"net_ev": "0.002"},
            output={"approved": True},
            evaluated_at=NOW + timedelta(milliseconds=210),
            duration_ms=2,
        ),
    )
    proposal = TradeProposalRecord(
        id=uuid4(),
        decision_cycle_id=cycle_id,
        experiment_id=experiment_id,
        symbol="BTCUSDT",
        direction=Direction.LONG,
        status=ProposalStatus.APPROVED,
        reason_code=ReasonCode.ACCEPTED,
        proposed_at=NOW + timedelta(milliseconds=230),
        expires_at=NOW + timedelta(minutes=1),
        entry_policy={"order_type": "market"},
        exit_policy={"stop_atr": "1", "target_atr": "1", "max_bars": 60},
        sizing_snapshot={"capital": "10000", "risk_fraction": "0.01"},
        approved_quantity=Decimal("0.001"),
        approved_notional=Decimal("60.05"),
        risk_at_stop=Decimal("0.60"),
    )
    return DecisionBundle(market_frame, cycle, agents, summary, gates, proposal)


def test_valid_bundle_is_canonical_and_hashable() -> None:
    bundle = _valid_bundle()

    bundle.validate()

    assert len(bundle.bundle_hash) == 64
    assert bundle.to_dict()["cycle"]["symbol"] == "BTCUSDT"  # type: ignore[index]


def test_bundle_requires_exactly_all_agents() -> None:
    bundle = _valid_bundle()
    incomplete = replace(bundle, agents=bundle.agents[:-1])

    with pytest.raises(ValueError, match="exactly one evaluation"):
        incomplete.validate()


def test_disabled_agent_must_explain_non_vote() -> None:
    bundle = _valid_bundle()
    bad = replace(
        bundle.agents[0],
        enabled=False,
        direction=Direction.NEUTRAL,
        reason_codes=(),
    )

    with pytest.raises(ValueError, match="disabled_agent"):
        replace(bundle, agents=(bad, *bundle.agents[1:])).validate()


def test_gate_sequence_and_failure_are_fail_closed() -> None:
    bundle = _valid_bundle()
    gates = (
        replace(
            bundle.gates[0],
            passed=False,
            reason_code=ReasonCode.DATA_QUALITY_FAILED,
        ),
        bundle.gates[1],
    )

    with pytest.raises(ValueError, match="passed after failure"):
        replace(bundle, gates=gates).validate()


def test_neutral_cycle_cannot_create_synthetic_proposal() -> None:
    bundle = _valid_bundle()
    neutral_cycle = replace(
        bundle.cycle,
        direction=Direction.NEUTRAL,
        disposition=Disposition.NEUTRAL,
        reason_code=ReasonCode.NEUTRAL_CONSENSUS,
    )

    with pytest.raises(ValueError, match="neutral cycle"):
        replace(bundle, cycle=neutral_cycle).validate()
