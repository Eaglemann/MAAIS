"""Authoritative restart snapshot for the official live paper runtime."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from uuid import UUID

from sqlalchemy import text

from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import StrategyStage
from maais.domain.json import JsonValue
from maais.experiments.manifest import ExperimentManifest
from maais.experiments.runtime_policy import LivePaperPolicy
from maais.market_data.history import CommittedFrameSnapshot
from maais.market_data.recovery import CursorStatus, MarketCursor, RecoveryState, RecoveryStatus


class RuntimeBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LivePaperRuntimeSnapshot:
    manifest: ExperimentManifest
    policy: LivePaperPolicy
    database_schema_revision: str
    strategy_version_id: UUID
    agent_version_ids: Mapping[str, UUID]
    cursors: tuple[MarketCursor, ...]
    history: tuple[CommittedFrameSnapshot, ...]
    recoveries: tuple[RecoveryState, ...]

    def __post_init__(self) -> None:
        if self.strategy_version_id.int == 0:
            raise ValueError("runtime strategy version ID cannot be nil")
        object.__setattr__(
            self,
            "agent_version_ids",
            MappingProxyType(dict(self.agent_version_ids)),
        )


async def restore_live_paper_runtime(
    uow: UnitOfWork,
    expected_manifest: ExperimentManifest,
) -> LivePaperRuntimeSnapshot:
    policy = LivePaperPolicy.from_manifest(expected_manifest)
    async with uow.begin() as transaction:
        stored_manifest = await transaction.experiments.get_manifest(
            expected_manifest.experiment_id
        )
        if (
            stored_manifest != expected_manifest
            or stored_manifest.manifest_hash != expected_manifest.manifest_hash
        ):
            raise RuntimeBootstrapError(
                "stored manifest differs from the requested live paper manifest"
            )
        revisions = tuple(
            str(value)
            for value in (
                await transaction.session.scalars(text("SELECT version_num FROM alembic_version"))
            ).all()
        )
        if len(revisions) != 1 or revisions[0] != expected_manifest.schema_revision:
            actual = ",".join(sorted(revisions)) if revisions else "missing"
            raise RuntimeBootstrapError(
                "database schema revision differs from manifest: "
                f"database={actual} manifest={expected_manifest.schema_revision}"
            )
        strategy_version_id = await transaction.experiments.register_strategy_version(
            strategy_key=policy.strategy_key,
            version=policy.strategy_version,
            stage=StrategyStage.SIMULATION,
            implementation_hash=policy.strategy_implementation_hash,
            parameters=cast(dict[str, JsonValue], dict(policy.strategy_parameters)),
        )
        agent_version_ids = await transaction.experiments.get_agent_version_ids(expected_manifest)
        cursors = await transaction.market_data.load_cursors(expected_manifest.experiment_id)
        recoveries = await transaction.market_data.get_blocking_recoveries(
            expected_manifest.experiment_id
        )
        history_rows: list[CommittedFrameSnapshot] = []
        for symbol in expected_manifest.symbols:
            history_rows.extend(
                await transaction.market_data.load_frame_history(
                    expected_manifest.experiment_id,
                    symbol,
                    "1m",
                    limit=policy.history_bars,
                )
            )
        history = tuple(history_rows)

    _validate_cursors(expected_manifest, cursors)
    _validate_recoveries(cursors, recoveries)
    return LivePaperRuntimeSnapshot(
        manifest=stored_manifest,
        policy=policy,
        database_schema_revision=revisions[0],
        strategy_version_id=strategy_version_id,
        agent_version_ids=agent_version_ids,
        cursors=cursors,
        history=history,
        recoveries=recoveries,
    )


def _validate_cursors(
    manifest: ExperimentManifest,
    cursors: tuple[MarketCursor, ...],
) -> None:
    identities: set[tuple[str, str, str, str]] = set()
    for cursor in cursors:
        identity = (cursor.venue, cursor.stream, cursor.symbol, cursor.timeframe)
        if identity in identities:
            raise RuntimeBootstrapError("restored market cursor identity is duplicated")
        identities.add(identity)
        if (
            cursor.experiment_id != manifest.experiment_id
            or cursor.symbol not in manifest.symbols
            or cursor.timeframe != "1m"
            or cursor.status is not CursorStatus.ACTIVE
        ):
            raise RuntimeBootstrapError(
                "restored market cursor differs from the live paper manifest"
            )


def _validate_recoveries(
    cursors: tuple[MarketCursor, ...],
    recoveries: tuple[RecoveryState, ...],
) -> None:
    cursor_by_identity = {
        (cursor.venue, cursor.stream, cursor.symbol, cursor.timeframe): cursor for cursor in cursors
    }
    seen: set[tuple[str, str, str, str]] = set()
    for recovery in recoveries:
        gap = recovery.gap
        identity = (gap.venue, gap.stream, gap.symbol, gap.timeframe)
        if identity in seen:
            raise RuntimeBootstrapError("multiple blocking recoveries exist for one cursor")
        seen.add(identity)
        if recovery.status is RecoveryStatus.FAILED:
            raise RuntimeBootstrapError(
                f"failed recovery requires operator review: {recovery.recovery_id}"
            )
        cursor = cursor_by_identity.get(identity)
        if cursor is None:
            raise RuntimeBootstrapError("active recovery has no persisted market cursor")
        progressed = recovery.dispatched_through_sequence
        expected_sequence = gap.start_sequence - 1 if progressed is None else progressed
        if cursor.sequence != expected_sequence or (
            progressed is not None and cursor.event_id != recovery.dispatched_through_event_id
        ):
            raise RuntimeBootstrapError(
                "active recovery progress differs from its persisted market cursor"
            )
