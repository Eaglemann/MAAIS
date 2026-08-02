from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, cast
from uuid import UUID

from maais.config.constants import ALL_AGENTS, AgentName
from maais.config.modes import RunMode
from maais.domain.enums import AgentMaturity
from maais.domain.json import JsonValue, content_hash, freeze_json, to_json_data

_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _require_hash(name: str, value: str, lengths: tuple[int, ...] = (64,)) -> None:
    if len(value) not in lengths or _HEX_RE.fullmatch(value) is None:
        expected = " or ".join(str(length) for length in lengths)
        raise ValueError(f"{name} must be {expected} lowercase hexadecimal characters")


def _freeze_mapping(name: str, value: Mapping[str, object]) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    return frozen


@dataclass(frozen=True, slots=True)
class AgentManifestEntry:
    agent_name: str
    version: str
    maturity: AgentMaturity
    weight: Decimal
    enabled: bool
    implementation_hash: str
    data_dependencies: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.agent_name not in ALL_AGENTS:
            raise ValueError(f"unknown agent: {self.agent_name}")
        if not self.version.strip():
            raise ValueError("agent version cannot be empty")
        if not self.weight.is_finite() or self.weight <= 0:
            raise ValueError("agent weight must be finite and positive")
        _require_hash("implementation_hash", self.implementation_hash)
        if self.enabled and self.maturity is AgentMaturity.DISABLED:
            raise ValueError("a disabled-maturity agent cannot be enabled")
        if not self.enabled and self.maturity is not AgentMaturity.DISABLED:
            raise ValueError("a disabled agent must have disabled maturity")
        object.__setattr__(
            self,
            "data_dependencies",
            _freeze_mapping("data_dependencies", self.data_dependencies),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_name": self.agent_name,
            "version": self.version,
            "maturity": self.maturity.value,
            "weight": str(self.weight),
            "enabled": self.enabled,
            "implementation_hash": self.implementation_hash,
            "data_dependencies": to_json_data(self.data_dependencies),
        }


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: UUID
    name: str
    mode: RunMode
    initial_capital: Decimal
    currency: str
    created_at: datetime
    git_sha: str
    worktree_hash: str | None
    lock_hash: str
    schema_revision: str
    configuration: Mapping[str, JsonValue]
    symbols: tuple[str, ...]
    exchange_metadata: Mapping[str, JsonValue]
    component_versions: Mapping[str, JsonValue]
    agent_versions: tuple[AgentManifestEntry, ...]
    fee_policy: Mapping[str, JsonValue]
    funding_policy: Mapping[str, JsonValue]
    clock_policy: Mapping[str, JsonValue]
    market_data_sources: Mapping[str, JsonValue]
    manifest_schema_version: int = 1

    def __post_init__(self) -> None:
        if self.experiment_id.int == 0:
            raise ValueError("experiment_id cannot be nil")
        if not self.name.strip():
            raise ValueError("experiment name cannot be empty")
        if not self.initial_capital.is_finite() or self.initial_capital <= 0:
            raise ValueError("initial capital must be finite and positive")
        if self.currency != "USDT":
            raise ValueError("the initial paper account currency must be USDT")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC-aware")
        _require_hash("git_sha", self.git_sha, (40, 64))
        if self.worktree_hash is not None:
            _require_hash("worktree_hash", self.worktree_hash)
        _require_hash("lock_hash", self.lock_hash)
        if not self.schema_revision.strip():
            raise ValueError("schema_revision cannot be empty")
        if self.manifest_schema_version != 1:
            raise ValueError("unsupported manifest_schema_version")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be non-empty and unique")
        if any(not symbol.strip() or symbol != symbol.upper() for symbol in self.symbols):
            raise ValueError("symbols must be non-empty uppercase venue symbols")

        agents_by_name = {entry.agent_name: entry for entry in self.agent_versions}
        if len(self.agent_versions) != len(ALL_AGENTS) or set(agents_by_name) != set(ALL_AGENTS):
            raise ValueError("manifest must contain exactly eight unique configured agents")
        macro = agents_by_name[AgentName.MACRO_SENTIMENT]
        if macro.maturity not in (AgentMaturity.PROXY, AgentMaturity.DISABLED):
            raise ValueError("macro_sentiment must be visibly proxy or disabled")
        object.__setattr__(
            self,
            "agent_versions",
            tuple(agents_by_name[name] for name in ALL_AGENTS),
        )

        for field_name in (
            "configuration",
            "exchange_metadata",
            "component_versions",
            "fee_policy",
            "funding_policy",
            "clock_policy",
            "market_data_sources",
        ):
            current = cast(Mapping[str, object], getattr(self, field_name))
            frozen = _freeze_mapping(field_name, current)
            if field_name != "configuration" and not frozen:
                raise ValueError(f"{field_name} cannot be empty")
            object.__setattr__(self, field_name, frozen)

    @property
    def config_hash(self) -> str:
        return content_hash(self.configuration)

    @property
    def manifest_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        result = to_json_data(
            {
                "manifest_schema_version": self.manifest_schema_version,
                "experiment_id": self.experiment_id,
                "name": self.name,
                "mode": self.mode,
                "initial_capital": self.initial_capital,
                "currency": self.currency,
                "created_at": self.created_at,
                "git_sha": self.git_sha,
                "worktree_hash": self.worktree_hash,
                "lock_hash": self.lock_hash,
                "schema_revision": self.schema_revision,
                "configuration": self.configuration,
                "symbols": self.symbols,
                "exchange_metadata": self.exchange_metadata,
                "component_versions": self.component_versions,
                "agent_versions": [entry.to_dict() for entry in self.agent_versions],
                "fee_policy": self.fee_policy,
                "funding_policy": self.funding_policy,
                "clock_policy": self.clock_policy,
                "market_data_sources": self.market_data_sources,
            }
        )
        if not isinstance(result, dict):
            raise TypeError("normalized manifest must be an object")
        return cast(dict[str, object], result)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExperimentManifest:
        agent_values = value.get("agent_versions")
        if not isinstance(agent_values, list):
            raise TypeError("agent_versions must be a list")
        agents: list[AgentManifestEntry] = []
        for item in agent_values:
            if not isinstance(item, Mapping):
                raise TypeError("agent manifest entry must be an object")
            dependencies = item.get("data_dependencies")
            if not isinstance(dependencies, Mapping):
                raise TypeError("agent data_dependencies must be an object")
            agents.append(
                AgentManifestEntry(
                    agent_name=str(item["agent_name"]),
                    version=str(item["version"]),
                    maturity=AgentMaturity(str(item["maturity"])),
                    weight=Decimal(str(item["weight"])),
                    enabled=bool(item["enabled"]),
                    implementation_hash=str(item["implementation_hash"]),
                    data_dependencies=cast(Mapping[str, JsonValue], dependencies),
                )
            )

        def mapping(name: str) -> Mapping[str, JsonValue]:
            item = value.get(name)
            if not isinstance(item, Mapping):
                raise TypeError(f"{name} must be an object")
            return cast(Mapping[str, JsonValue], item)

        raw_created_at = str(value["created_at"]).replace("Z", "+00:00")
        symbols = value.get("symbols")
        if not isinstance(symbols, list):
            raise TypeError("symbols must be a list")
        return cls(
            experiment_id=UUID(str(value["experiment_id"])),
            name=str(value["name"]),
            mode=RunMode(str(value["mode"])),
            initial_capital=Decimal(str(value["initial_capital"])),
            currency=str(value["currency"]),
            created_at=datetime.fromisoformat(raw_created_at),
            git_sha=str(value["git_sha"]),
            worktree_hash=(
                str(value["worktree_hash"]) if value.get("worktree_hash") is not None else None
            ),
            lock_hash=str(value["lock_hash"]),
            schema_revision=str(value["schema_revision"]),
            configuration=mapping("configuration"),
            symbols=tuple(str(symbol) for symbol in symbols),
            exchange_metadata=mapping("exchange_metadata"),
            component_versions=mapping("component_versions"),
            agent_versions=tuple(agents),
            fee_policy=mapping("fee_policy"),
            funding_policy=mapping("funding_policy"),
            clock_policy=mapping("clock_policy"),
            market_data_sources=mapping("market_data_sources"),
            manifest_schema_version=int(str(value["manifest_schema_version"])),
        )


def build_manifest(**values: object) -> ExperimentManifest:
    """Named factory used by CLI/config adapters after collecting all identity inputs."""

    return ExperimentManifest(**values)  # type: ignore[arg-type]


def require_candidate_identity(manifest: ExperimentManifest) -> ExperimentManifest:
    if manifest.worktree_hash is not None:
        raise ValueError("official candidates require a clean committed worktree")
    _require_hash("git_sha", manifest.git_sha, (40, 64))
    return manifest
