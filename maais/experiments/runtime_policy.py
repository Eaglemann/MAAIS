"""Strict immutable policy extraction for the official live paper runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from maais.config.modes import RunMode
from maais.experiments.manifest import ExperimentManifest
from maais.market_data.integrity.state_machine import IntegrityPolicy

_REQUIRED_COMPONENT_VERSIONS = frozenset(
    {
        "features",
        "integrity",
        "decision",
        "monitoring",
        "risk",
        "exit",
        "fill",
        "protection",
        "counterfactual",
    }
)
_OFFICIAL_SOURCES = {
    "futures": "binance_usdm",
    "primary_spot": "binance_spot",
    "secondary_venue": "bybit_spot",
}


class RuntimePolicyError(ValueError):
    pass


def _mapping(parent: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = parent.get(name)
    if not isinstance(value, Mapping):
        raise RuntimePolicyError(f"{name} must be an explicit object")
    return value


def _text(parent: Mapping[str, object], name: str) -> str:
    value = parent.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimePolicyError(f"{name} must be an explicit nonempty string")
    return value


def _decimal(parent: Mapping[str, object], name: str) -> Decimal:
    value = parent.get(name)
    if isinstance(value, bool) or value is None:
        raise RuntimePolicyError(f"{name} must be an explicit decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimePolicyError(f"{name} must be an explicit decimal") from exc
    if not parsed.is_finite():
        raise RuntimePolicyError(f"{name} must be finite")
    return parsed


def _integer(parent: Mapping[str, object], name: str) -> int:
    value = parent.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimePolicyError(f"{name} must be an explicit integer")
    return value


@dataclass(frozen=True, slots=True)
class LivePaperPolicy:
    proposal_ttl: timedelta
    execution_latency: timedelta
    maximum_decision_lag: timedelta
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    leverage: int
    history_bars: int
    benchmark_symbol: str
    benchmark_horizon_bars: int
    benchmark_source: str

    @classmethod
    def from_manifest(cls, manifest: ExperimentManifest) -> LivePaperPolicy:
        if manifest.mode is not RunMode.PAPER_LIVE:
            raise RuntimePolicyError("live paper runtime requires mode=paper_live")

        risk = _mapping(manifest.configuration, "risk")
        leverage = _integer(risk, "leverage")
        if leverage != 1:
            raise RuntimePolicyError("official paper runtime requires leverage exactly 1")

        runtime = _mapping(manifest.configuration, "runtime")
        proposal_ttl_seconds = _decimal(runtime, "proposal_ttl_seconds")
        history_bars = _integer(runtime, "history_bars")
        if proposal_ttl_seconds <= 0 or proposal_ttl_seconds > 300:
            raise RuntimePolicyError("proposal_ttl_seconds must be in (0, 300]")
        if history_bars < 60 or history_bars > 10_000:
            raise RuntimePolicyError("history_bars must be in [60, 10000]")

        benchmark = _mapping(manifest.configuration, "benchmark")
        benchmark_symbol = _text(benchmark, "symbol")
        benchmark_horizon_bars = _integer(benchmark, "horizon_bars")
        benchmark_source = _text(benchmark, "source")
        if benchmark_symbol != benchmark_symbol.upper():
            raise RuntimePolicyError("benchmark symbol must be uppercase")
        if benchmark_symbol not in manifest.symbols:
            raise RuntimePolicyError("benchmark symbol must be subscribed by the experiment")
        if not 2 <= benchmark_horizon_bars <= history_bars:
            raise RuntimePolicyError("benchmark horizon must fit the retained history")

        latency_ms = _integer(manifest.clock_policy, "latency_ms")
        decision_lag_ms = _integer(manifest.clock_policy, "maximum_decision_lag_ms")
        if latency_ms <= 0 or latency_ms > 60_000:
            raise RuntimePolicyError("latency_ms must be in (0, 60000]")
        if decision_lag_ms <= 0 or decision_lag_ms > 60_000:
            raise RuntimePolicyError("maximum_decision_lag_ms must be in (0, 60000]")

        maker = _decimal(manifest.fee_policy, "maker")
        taker = _decimal(manifest.fee_policy, "taker")
        if maker < 0 or taker < maker or taker > Decimal("0.01"):
            raise RuntimePolicyError("fees must satisfy 0 <= maker <= taker <= 0.01")

        if _text(manifest.funding_policy, "source") != "observed":
            raise RuntimePolicyError("official funding source must be observed")
        for name, expected in _OFFICIAL_SOURCES.items():
            if _text(manifest.market_data_sources, name) != expected:
                raise RuntimePolicyError(f"official {name} source must be {expected}")

        versions = {
            name
            for name, version in manifest.component_versions.items()
            if isinstance(version, str) and version.strip()
        }
        missing_versions = sorted(_REQUIRED_COMPONENT_VERSIONS - versions)
        if missing_versions:
            raise RuntimePolicyError(f"component versions missing: {', '.join(missing_versions)}")

        return cls(
            proposal_ttl=timedelta(seconds=float(proposal_ttl_seconds)),
            execution_latency=timedelta(milliseconds=latency_ms),
            maximum_decision_lag=timedelta(milliseconds=decision_lag_ms),
            maker_fee_rate=maker,
            taker_fee_rate=taker,
            leverage=leverage,
            history_bars=history_bars,
            benchmark_symbol=benchmark_symbol,
            benchmark_horizon_bars=benchmark_horizon_bars,
            benchmark_source=benchmark_source,
        )

    def integrity_policy(self) -> IntegrityPolicy:
        return replace(
            IntegrityPolicy.official(),
            max_decision_lag=self.maximum_decision_lag,
        )
