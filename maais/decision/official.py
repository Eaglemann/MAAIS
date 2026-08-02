from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from maais.agents.evaluations import AgentEvaluationMatrix
from maais.config.fees import BINANCE_USDM_REGULAR_TAKER_FEE_RATE
from maais.domain.enums import Direction
from maais.domain.json import JsonValue, content_hash, freeze_json
from maais.feature_pipeline.features import FeatureSet


@dataclass(frozen=True, slots=True)
class OfficialDecisionPolicy:
    adversarial_block_threshold: Decimal
    round_trip_fee_fraction: Decimal
    atr_slippage_multiplier: Decimal
    spread_impact_multiplier: Decimal
    maximum_expected_move_fraction: Decimal
    minimum_directional_voters: int

    def __post_init__(self) -> None:
        for name in (
            "adversarial_block_threshold",
            "round_trip_fee_fraction",
            "atr_slippage_multiplier",
            "spread_impact_multiplier",
            "maximum_expected_move_fraction",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        if self.adversarial_block_threshold > 1:
            raise ValueError("adversarial threshold cannot exceed one")
        if self.minimum_directional_voters <= 0:
            raise ValueError("minimum directional voters must be positive")

    @classmethod
    def conservative(
        cls,
        *,
        taker_fee_fraction: Decimal = BINANCE_USDM_REGULAR_TAKER_FEE_RATE,
    ) -> OfficialDecisionPolicy:
        if (
            not taker_fee_fraction.is_finite()
            or taker_fee_fraction < 0
            or taker_fee_fraction > Decimal("0.01")
        ):
            raise ValueError("taker fee fraction must be a finite Decimal in [0, 0.01]")
        return cls(
            adversarial_block_threshold=Decimal("0.65"),
            round_trip_fee_fraction=taker_fee_fraction * 2,
            atr_slippage_multiplier=Decimal("0.5"),
            spread_impact_multiplier=Decimal("0.3"),
            maximum_expected_move_fraction=Decimal("0.20"),
            minimum_directional_voters=2,
        )


@dataclass(frozen=True, slots=True)
class OfficialDecisionAnalysis:
    direction: Direction
    consensus_probability: Decimal
    consensus_confidence: Decimal
    long_weight: Decimal
    short_weight: Decimal
    neutral_weight: Decimal
    directional_voters: int
    consensus_passed: bool
    consensus_reason: str
    dissenters: tuple[str, ...]
    dissent_probability: Decimal
    dissent_confidence: Decimal
    challenge_blocked: bool
    expected_gain: Decimal
    expected_loss: Decimal
    gross_ev: Decimal
    funding_carry: Decimal
    estimated_cost: Decimal
    net_ev: Decimal
    ev_positive: bool
    ev_reason: str
    benchmark_available: bool
    benchmark_return: Decimal
    alpha_estimate: Decimal
    alpha_positive: bool
    alpha_reason: str
    consensus_snapshot: Mapping[str, JsonValue]
    adversarial_snapshot: Mapping[str, JsonValue]
    ev_snapshot: Mapping[str, JsonValue]
    cost_snapshot: Mapping[str, JsonValue]
    content_hash: str

    def __post_init__(self) -> None:
        for name in (
            "consensus_probability",
            "consensus_confidence",
            "dissent_probability",
            "dissent_confidence",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0 or value > 1:
                raise ValueError(f"{name} must be a finite Decimal in [0, 1]")
        for name in (
            "long_weight",
            "short_weight",
            "neutral_weight",
            "expected_gain",
            "expected_loss",
            "estimated_cost",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        for name in ("gross_ev", "funding_carry", "net_ev", "benchmark_return", "alpha_estimate"):
            if not getattr(self, name).is_finite():
                raise ValueError(f"{name} must be finite")
        if self.direction is Direction.NEUTRAL and self.consensus_passed:
            raise ValueError("neutral consensus cannot pass")
        if self.ev_positive != (self.net_ev > 0):
            raise ValueError("EV verdict differs from net EV")
        if self.alpha_positive != (self.benchmark_available and self.alpha_estimate > 0):
            raise ValueError("alpha verdict differs from alpha estimate")
        if len(self.content_hash) != 64:
            raise ValueError("official decision content hash must be SHA-256")
        for name in (
            "consensus_snapshot",
            "adversarial_snapshot",
            "ev_snapshot",
            "cost_snapshot",
        ):
            normalized = freeze_json(getattr(self, name))
            if not isinstance(normalized, Mapping):
                raise TypeError(f"{name} must be an object")
            object.__setattr__(self, name, normalized)


def _object(value: object) -> Mapping[str, JsonValue]:
    normalized = freeze_json(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("official decision snapshot must be an object")
    return normalized


def _feature_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


class OfficialDecisionAnalytics:
    def __init__(self, policy: OfficialDecisionPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        *,
        features: FeatureSet,
        matrix: AgentEvaluationMatrix,
        executable_price: Decimal,
        benchmark_return: Decimal | None,
    ) -> OfficialDecisionAnalysis:
        if not executable_price.is_finite() or executable_price <= 0:
            raise ValueError("executable price must be a positive finite Decimal")
        if benchmark_return is not None and not benchmark_return.is_finite():
            raise ValueError("benchmark return must be a finite Decimal")
        if (
            matrix.symbol != features.symbol
            or matrix.timeframe != features.timeframe
            or matrix.feature_at != features.timestamp
            or matrix.regime != features.regime
        ):
            raise ValueError("agent matrix and feature identity differ")

        directional = tuple(
            item
            for item in matrix.evaluations
            if item.voting and item.output.direction in {Direction.LONG, Direction.SHORT}
        )
        long_rows = tuple(item for item in directional if item.output.direction is Direction.LONG)
        short_rows = tuple(item for item in directional if item.output.direction is Direction.SHORT)
        neutral_rows = tuple(
            item
            for item in matrix.evaluations
            if not item.voting or item.output.direction is Direction.NEUTRAL
        )
        long_weight = sum((item.weight for item in long_rows), start=Decimal("0"))
        short_weight = sum((item.weight for item in short_rows), start=Decimal("0"))
        neutral_weight = sum((item.weight for item in neutral_rows), start=Decimal("0"))
        if len(directional) < self._policy.minimum_directional_voters:
            direction = Direction.NEUTRAL
            consensus_reason = "insufficient_directional_voters"
        elif long_weight == short_weight:
            direction = Direction.NEUTRAL
            consensus_reason = "directional_weight_tie"
        elif long_weight > short_weight:
            direction = Direction.LONG
            consensus_reason = "long_weight_majority"
        else:
            direction = Direction.SHORT
            consensus_reason = "short_weight_majority"
        winners = long_rows if direction is Direction.LONG else short_rows
        winner_weight = sum((item.weight for item in winners), start=Decimal("0"))
        if direction is Direction.NEUTRAL or winner_weight == 0:
            probability = Decimal("0.5")
            confidence = Decimal("0")
        else:
            probability = (
                sum(
                    (item.weight * item.output.probability for item in winners),
                    start=Decimal("0"),
                )
                / winner_weight
            )
            confidence = (
                sum(
                    (item.weight * item.output.confidence for item in winners),
                    start=Decimal("0"),
                )
                / winner_weight
            )
        consensus_passed = direction is not Direction.NEUTRAL

        minority_direction = Direction.SHORT if direction is Direction.LONG else Direction.LONG
        dissenters = tuple(
            item for item in directional if item.output.direction is minority_direction
        )
        dissent_weight = sum((item.weight for item in dissenters), start=Decimal("0"))
        if direction is Direction.NEUTRAL or dissent_weight == 0:
            dissent_probability = Decimal("0")
            dissent_confidence = Decimal("0")
        else:
            dissent_probability = (
                sum(
                    (item.weight * item.output.probability for item in dissenters),
                    start=Decimal("0"),
                )
                / dissent_weight
            )
            dissent_confidence = (
                sum(
                    (item.weight * item.output.confidence for item in dissenters),
                    start=Decimal("0"),
                )
                / dissent_weight
            )
        challenge_blocked = (
            consensus_passed
            and bool(dissenters)
            and dissent_confidence >= self._policy.adversarial_block_threshold
        )

        atr = _feature_decimal(features.atr)
        spread = _feature_decimal(features.bid_ask_spread)
        expected_move_available = (
            atr is not None
            and atr > 0
            and atr / executable_price <= self._policy.maximum_expected_move_fraction
        )
        cost_inputs_available = spread is not None and spread >= 0
        if direction is Direction.NEUTRAL or not expected_move_available:
            expected_gain = Decimal("0")
            expected_loss = Decimal("0")
            gross_ev = Decimal("0")
            funding_carry = Decimal("0")
            estimated_cost = Decimal("0")
            net_ev = Decimal("0")
            ev_reason = (
                "neutral_consensus"
                if direction is Direction.NEUTRAL
                else (
                    "expected_move_unavailable"
                    if atr is None or atr <= 0
                    else "expected_move_out_of_bounds"
                )
            )
        elif not cost_inputs_available:
            if atr is None:
                raise RuntimeError("available expected move unexpectedly has no ATR")
            expected_gain = atr / executable_price
            expected_loss = atr / executable_price
            gross_ev = Decimal("0")
            funding_carry = Decimal("0")
            estimated_cost = Decimal("0")
            net_ev = Decimal("0")
            ev_reason = "cost_inputs_unavailable"
        else:
            if atr is None:
                raise RuntimeError("available expected move unexpectedly has no ATR")
            expected_gain = atr / executable_price
            expected_loss = atr / executable_price
            gross_ev = probability * expected_gain - (Decimal("1") - probability) * expected_loss
            funding_carry = self._funding_carry(features, direction)
            slippage = expected_gain * self._policy.atr_slippage_multiplier
            if spread is not None and spread >= 0:
                slippage += spread * self._policy.spread_impact_multiplier
            estimated_cost = self._policy.round_trip_fee_fraction + slippage
            net_ev = gross_ev + funding_carry - estimated_cost
            ev_reason = "positive_ev" if net_ev > 0 else "non_positive_ev"
        ev_positive = net_ev > 0
        benchmark_available = benchmark_return is not None
        benchmark_value = benchmark_return if benchmark_return is not None else Decimal("0")
        alpha_estimate = net_ev - benchmark_value
        alpha_positive = benchmark_available and alpha_estimate > 0
        if not benchmark_available:
            alpha_reason = "benchmark_unavailable"
        else:
            alpha_reason = "positive_alpha" if alpha_positive else "non_positive_alpha"

        consensus_snapshot = _object(
            {
                "direction": direction,
                "reason": consensus_reason,
                "probability": probability,
                "confidence": confidence,
                "long_weight": long_weight,
                "short_weight": short_weight,
                "neutral_weight": neutral_weight,
                "directional_voters": len(directional),
                "matrix_hash": matrix.content_hash,
            }
        )
        adversarial_snapshot = _object(
            {
                "majority_direction": direction,
                "minority_direction": (
                    minority_direction if direction is not Direction.NEUTRAL else Direction.NEUTRAL
                ),
                "dissenters": [item.agent_name for item in dissenters],
                "dissent_probability": dissent_probability,
                "dissent_confidence": dissent_confidence,
                "challenge_blocked": challenge_blocked,
                "threshold": self._policy.adversarial_block_threshold,
            }
        )
        cost_snapshot = _object(
            {
                "round_trip_fee_fraction": self._policy.round_trip_fee_fraction,
                "atr_slippage_multiplier": self._policy.atr_slippage_multiplier,
                "spread_impact_multiplier": self._policy.spread_impact_multiplier,
                "maximum_expected_move_fraction": (self._policy.maximum_expected_move_fraction),
                "spread_fraction": spread,
                "estimated_cost": estimated_cost,
            }
        )
        ev_snapshot = _object(
            {
                "p_win": probability,
                "p_loss": Decimal("1") - probability,
                "expected_gain": expected_gain,
                "expected_loss": expected_loss,
                "gross_ev": gross_ev,
                "funding_carry": funding_carry,
                "estimated_cost": estimated_cost,
                "net_ev": net_ev,
                "ev_reason": ev_reason,
                "benchmark_available": benchmark_available,
                "benchmark_return": benchmark_return,
                "alpha_estimate": alpha_estimate,
                "alpha_reason": alpha_reason,
            }
        )
        normalized = {
            "consensus": consensus_snapshot,
            "adversarial": adversarial_snapshot,
            "cost": cost_snapshot,
            "ev": ev_snapshot,
        }
        return OfficialDecisionAnalysis(
            direction=direction,
            consensus_probability=probability,
            consensus_confidence=confidence,
            long_weight=long_weight,
            short_weight=short_weight,
            neutral_weight=neutral_weight,
            directional_voters=len(directional),
            consensus_passed=consensus_passed,
            consensus_reason=consensus_reason,
            dissenters=tuple(item.agent_name for item in dissenters),
            dissent_probability=dissent_probability,
            dissent_confidence=dissent_confidence,
            challenge_blocked=challenge_blocked,
            expected_gain=expected_gain,
            expected_loss=expected_loss,
            gross_ev=gross_ev,
            funding_carry=funding_carry,
            estimated_cost=estimated_cost,
            net_ev=net_ev,
            ev_positive=ev_positive,
            ev_reason=ev_reason,
            benchmark_available=benchmark_available,
            benchmark_return=benchmark_value,
            alpha_estimate=alpha_estimate,
            alpha_positive=alpha_positive,
            alpha_reason=alpha_reason,
            consensus_snapshot=consensus_snapshot,
            adversarial_snapshot=adversarial_snapshot,
            ev_snapshot=ev_snapshot,
            cost_snapshot=cost_snapshot,
            content_hash=content_hash(normalized),
        )

    @staticmethod
    def _funding_carry(features: FeatureSet, direction: Direction) -> Decimal:
        annualized = _feature_decimal(features.annualized_funding)
        if annualized is None or features.funding_bias not in {"long_heavy", "short_heavy"}:
            return Decimal("0")
        per_period = abs(annualized) / Decimal(365 * 3)
        rewarded = (
            direction is Direction.SHORT
            if features.funding_bias == "long_heavy"
            else direction is Direction.LONG
        )
        return per_period if rewarded else -per_period
