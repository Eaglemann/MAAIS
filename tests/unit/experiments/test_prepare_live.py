from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from maais.config.constants import ALL_AGENTS, TRADING_PAIRS
from maais.config.modes import RunMode
from maais.experiments.manifest import require_candidate_identity
from maais.experiments.prepare import (
    RepositoryIdentity,
    capture_repository_identity,
    prepare_live_paper_manifest,
)
from maais.experiments.runtime_policy import LivePaperPolicy
from tests.unit.experiments.test_runtime_policy import _live_filter

NOW = datetime(2026, 8, 2, 15, tzinfo=timezone.utc)


def _identity(*, dirty: bool = False) -> RepositoryIdentity:
    return RepositoryIdentity(
        git_sha="1" * 40,
        worktree_hash="2" * 64 if dirty else None,
        lock_hash="3" * 64,
        schema_revision="0015",
        agent_implementation_hashes={
            name: f"{index + 1:064x}" for index, name in enumerate(ALL_AGENTS)
        },
    )


def test_prepare_live_manifest_pins_complete_runtime_identity() -> None:
    filters = tuple(_live_filter(symbol) for symbol in TRADING_PAIRS)
    identity = _identity()

    manifest = prepare_live_paper_manifest(
        name="seven-day development candidate",
        experiment_id=UUID(int=401),
        created_at=NOW,
        repository=identity,
        exchange_filters=filters,
        primary_mapping_hash="4" * 64,
        secondary_mapping_hash="5" * 64,
    )
    policy = LivePaperPolicy.from_manifest(manifest)

    assert manifest.mode is RunMode.PAPER_LIVE
    assert manifest.symbols == TRADING_PAIRS
    assert manifest.initial_capital == 10_000
    assert manifest.worktree_hash is None
    assert policy.exchange_filters == {item.symbol: item for item in filters}
    assert policy.strategy_parameters["timeframe"] == "1m"
    assert manifest.exchange_metadata["primary_mapping_hash"] == "4" * 64
    assert manifest.exchange_metadata["secondary_mapping_hash"] == "5" * 64
    assert require_candidate_identity(manifest) == manifest


def test_prepare_live_manifest_marks_dirty_development_identity_and_checks_coverage() -> None:
    filters = tuple(_live_filter(symbol) for symbol in TRADING_PAIRS)
    dirty = prepare_live_paper_manifest(
        name="dirty development run",
        experiment_id=UUID(int=402),
        created_at=NOW,
        repository=_identity(dirty=True),
        exchange_filters=filters,
        primary_mapping_hash="4" * 64,
        secondary_mapping_hash="5" * 64,
    )

    assert dirty.worktree_hash == "2" * 64
    with pytest.raises(ValueError, match="clean committed worktree"):
        require_candidate_identity(dirty)
    with pytest.raises(ValueError, match="exact configured symbols"):
        prepare_live_paper_manifest(
            name="incomplete filters",
            experiment_id=UUID(int=403),
            created_at=NOW,
            repository=_identity(),
            exchange_filters=filters[:-1],
            primary_mapping_hash="4" * 64,
            secondary_mapping_hash="5" * 64,
        )


def test_repository_identity_hashes_dirty_content_lock_and_agent_sources(
    tmp_path: Path,
) -> None:
    (tmp_path / "uv.lock").write_bytes(b"locked dependencies")
    agent_sources: dict[str, Path] = {}
    for index, name in enumerate(ALL_AGENTS):
        path = tmp_path / f"agent-{index}.py"
        path.write_text(f"AGENT = {name!r}\n")
        agent_sources[name] = path
    untracked = tmp_path / "new.py"
    untracked.write_text("NEW = True\n")

    def git(arguments: tuple[str, ...], root: Path) -> bytes:
        assert root == tmp_path
        outputs = {
            ("rev-parse", "HEAD"): b"a" * 40 + b"\n",
            (
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ): b"?? new.py\0",
            ("diff", "--binary", "HEAD"): b"tracked diff",
            ("ls-files", "--others", "--exclude-standard", "-z"): b"new.py\0",
        }
        return outputs[arguments]

    identity = capture_repository_identity(
        tmp_path,
        schema_revision="0015",
        git_runner=git,
        agent_sources=agent_sources,
    )

    assert identity.git_sha == "a" * 40
    assert identity.worktree_hash is not None
    assert len(identity.worktree_hash) == 64
    assert len(identity.lock_hash) == 64
    assert set(identity.agent_implementation_hashes) == set(ALL_AGENTS)
