"""Pure construction of a fully pinned live-paper experiment manifest."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Protocol
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory

from maais.config.constants import ALL_AGENTS, TRADING_PAIRS, AgentName
from maais.config.modes import RunMode
from maais.domain.enums import AgentMaturity
from maais.domain.json import content_hash, freeze_json
from maais.execution.paper.filters import ExchangeFilterSnapshot
from maais.experiments.manifest import AgentManifestEntry, ExperimentManifest

_COMPONENTS = (
    "features",
    "integrity",
    "decision",
    "monitoring",
    "risk",
    "exit",
    "fill",
    "protection",
    "counterfactual",
)
_AGENT_SOURCES = {
    AgentName.MOMENTUM: "maais/agents/momentum.py",
    AgentName.TECHNICAL_STRUCTURE: "maais/agents/technical_structure.py",
    AgentName.LIQUIDITY: "maais/agents/liquidity.py",
    AgentName.ORDER_FLOW_TOXICITY: "maais/agents/order_flow_toxicity.py",
    AgentName.STOP_RUN_DETECTION: "maais/agents/stop_run_detection.py",
    AgentName.MEAN_REVERSION: "maais/agents/mean_reversion.py",
    AgentName.CARRY_YIELD: "maais/agents/carry_yield.py",
    AgentName.MACRO_SENTIMENT: "maais/agents/macro_sentiment.py",
}
GitRunner = Callable[[tuple[str, ...], Path], bytes]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    git_sha: str
    worktree_hash: str | None
    lock_hash: str
    schema_revision: str
    agent_implementation_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        _hex_hash("git_sha", self.git_sha, lengths=(40, 64))
        if self.worktree_hash is not None:
            _hex_hash("worktree_hash", self.worktree_hash)
        _hex_hash("lock_hash", self.lock_hash)
        if not self.schema_revision:
            raise ValueError("schema_revision is required")
        hashes = dict(self.agent_implementation_hashes)
        if set(hashes) != set(ALL_AGENTS):
            raise ValueError("repository identity requires exact agent implementation hashes")
        for name, value in hashes.items():
            _hex_hash(f"agent implementation hash for {name}", value)
        object.__setattr__(
            self,
            "agent_implementation_hashes",
            MappingProxyType(hashes),
        )


def capture_repository_identity(
    root: Path,
    *,
    schema_revision: str | None = None,
    git_runner: GitRunner | None = None,
    agent_sources: Mapping[str, Path] | None = None,
) -> RepositoryIdentity:
    repository_root = root.resolve(strict=True)
    run_git = git_runner or _run_git
    git_sha = run_git(("rev-parse", "HEAD"), repository_root).decode("ascii").strip()
    status = run_git(
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        repository_root,
    )
    worktree_hash = None
    if status:
        digest = hashlib.sha256()
        _hash_chunk(digest, b"status", status)
        _hash_chunk(
            digest,
            b"tracked-diff",
            run_git(("diff", "--binary", "HEAD"), repository_root),
        )
        untracked = run_git(
            ("ls-files", "--others", "--exclude-standard", "-z"),
            repository_root,
        )
        for raw_path in sorted(item for item in untracked.split(b"\0") if item):
            relative = Path(raw_path.decode("utf-8", errors="surrogateescape"))
            candidate = (repository_root / relative).resolve(strict=True)
            try:
                candidate.relative_to(repository_root)
            except ValueError as exc:
                raise ValueError("untracked repository path escapes the worktree") from exc
            _hash_chunk(digest, raw_path, candidate.read_bytes())
        worktree_hash = digest.hexdigest()

    lock_path = repository_root / "uv.lock"
    sources = agent_sources or {
        name: repository_root / relative for name, relative in _AGENT_SOURCES.items()
    }
    if set(sources) != set(ALL_AGENTS):
        raise ValueError("agent source paths must cover every configured agent")
    agent_hashes = {
        name: hashlib.sha256(_safe_source(repository_root, path).read_bytes()).hexdigest()
        for name, path in sources.items()
    }
    return RepositoryIdentity(
        git_sha=git_sha,
        worktree_hash=worktree_hash,
        lock_hash=hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        schema_revision=schema_revision or _single_schema_head(repository_root),
        agent_implementation_hashes=agent_hashes,
    )


def prepare_live_paper_manifest(
    *,
    name: str,
    experiment_id: UUID,
    created_at: datetime,
    repository: RepositoryIdentity,
    exchange_filters: Sequence[ExchangeFilterSnapshot],
    primary_mapping_hash: str,
    secondary_mapping_hash: str,
) -> ExperimentManifest:
    _hex_hash("primary_mapping_hash", primary_mapping_hash)
    _hex_hash("secondary_mapping_hash", secondary_mapping_hash)
    filters = tuple(exchange_filters)
    by_symbol = {item.symbol: item for item in filters}
    if len(by_symbol) != len(filters) or set(by_symbol) != set(TRADING_PAIRS):
        raise ValueError("exchange filters must cover exact configured symbols")

    strategy_hash = content_hash(
        {
            "strategy_key": "maais_primary",
            "git_sha": repository.git_sha,
            "worktree_hash": repository.worktree_hash,
        }
    )
    version = f"git-{repository.git_sha[:12]}"
    agents = tuple(
        AgentManifestEntry(
            agent_name=agent_name,
            version=version,
            maturity=(
                AgentMaturity.PROXY
                if agent_name == AgentName.MACRO_SENTIMENT
                else AgentMaturity.IMPLEMENTED
            ),
            weight=Decimal("1"),
            enabled=True,
            implementation_hash=repository.agent_implementation_hashes[agent_name],
            data_dependencies={"market_frame": "v1"},
        )
        for agent_name in ALL_AGENTS
    )
    exchange_metadata = freeze_json(
        {
            "venue": "binance_usdm",
            "market": "usdt_perpetual",
            "filter_snapshot_hashes": {
                symbol: by_symbol[symbol].content_hash for symbol in TRADING_PAIRS
            },
            "filter_snapshots": {
                symbol: by_symbol[symbol].to_dict() for symbol in TRADING_PAIRS
            },
            "primary_mapping_hash": primary_mapping_hash,
            "secondary_mapping_hash": secondary_mapping_hash,
        }
    )
    if not isinstance(exchange_metadata, Mapping):
        raise TypeError("exchange metadata must be an object")
    return ExperimentManifest(
        experiment_id=experiment_id,
        name=name,
        mode=RunMode.PAPER_LIVE,
        initial_capital=Decimal("10000"),
        currency="USDT",
        created_at=created_at,
        git_sha=repository.git_sha,
        worktree_hash=repository.worktree_hash,
        lock_hash=repository.lock_hash,
        schema_revision=repository.schema_revision,
        configuration={
            "risk": {"leverage": 1},
            "runtime": {
                "proposal_ttl_seconds": "30",
                "book_wait_timeout_seconds": "5",
                "history_bars": 240,
            },
            "benchmark": {
                "symbol": "BTCUSDT",
                "horizon_bars": 60,
                "source": "binance_spot_close",
            },
            "strategy": {
                "key": "maais_primary",
                "version": version,
                "stage": "simulation",
                "implementation_hash": strategy_hash,
                "parameters": {"timeframe": "1m"},
            },
        },
        symbols=TRADING_PAIRS,
        exchange_metadata=exchange_metadata,
        component_versions={name: version for name in _COMPONENTS},
        agent_versions=agents,
        fee_policy={"maker": "0.0002", "taker": "0.0004"},
        funding_policy={"source": "observed"},
        clock_policy={"latency_ms": 250, "maximum_decision_lag_ms": 5000},
        market_data_sources={
            "futures": "binance_usdm",
            "primary_spot": "binance_spot",
            "secondary_venue": "bybit_spot",
        },
    )


def _hex_hash(name: str, value: str, *, lengths: tuple[int, ...] = (64,)) -> None:
    if len(value) not in lengths or any(character not in "0123456789abcdef" for character in value):
        expected = " or ".join(str(length) for length in lengths)
        raise ValueError(f"{name} must be {expected} lowercase hexadecimal characters")


def _run_git(arguments: tuple[str, ...], root: Path) -> bytes:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _hash_chunk(digest: _Digest, name: bytes, content: bytes) -> None:
    digest.update(len(name).to_bytes(8, "big"))
    digest.update(name)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _safe_source(root: Path, path: Path) -> Path:
    candidate = path.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("agent source path escapes the repository") from exc
    return candidate


def _single_schema_head(root: Path) -> str:
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise ValueError("runtime preparation requires exactly one Alembic head")
    return heads[0]
