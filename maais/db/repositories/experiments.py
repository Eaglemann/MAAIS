from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maais.config.constants import ALL_AGENTS
from maais.db.models.experiments import AgentVersionModel, ExperimentModel, StrategyVersionModel
from maais.db.repositories.events import EventRepository
from maais.domain.enums import ExperimentStatus, StrategyStage
from maais.domain.events import NewDomainEvent
from maais.domain.json import JsonValue, MutableJsonValue, freeze_json, to_json_data
from maais.experiments.manifest import AgentManifestEntry, ExperimentManifest
from maais.experiments.service import ExperimentLifecycle, ExperimentTransition


class ImmutableManifestError(RuntimeError):
    pass


class VersionIdentityConflict(RuntimeError):
    pass


def _json_object(value: object) -> dict[str, MutableJsonValue]:
    normalized = to_json_data(value)
    if not isinstance(normalized, dict):
        raise TypeError("expected a JSON object")
    return normalized


class ExperimentRepository:
    def __init__(self, session: AsyncSession, events: EventRepository) -> None:
        self._session = session
        self._events = events

    async def register_strategy_version(
        self,
        *,
        strategy_key: str,
        version: str,
        stage: StrategyStage,
        implementation_hash: str,
        parameters: dict[str, JsonValue],
    ) -> UUID:
        existing = await self._session.scalar(
            select(StrategyVersionModel).where(
                StrategyVersionModel.strategy_key == strategy_key,
                StrategyVersionModel.version == version,
            )
        )
        expected_parameters = _json_object(parameters)
        if existing is not None:
            if (
                existing.stage != stage.value
                or existing.implementation_hash != implementation_hash
                or existing.parameter_json != expected_parameters
            ):
                raise VersionIdentityConflict(
                    f"strategy version conflict: {strategy_key}/{version}"
                )
            return existing.id
        model = StrategyVersionModel(
            strategy_key=strategy_key,
            version=version,
            stage=stage.value,
            implementation_hash=implementation_hash,
            parameter_json=expected_parameters,
        )
        self._session.add(model)
        await self._session.flush()
        return model.id

    async def register_agent_version(
        self,
        entry: AgentManifestEntry,
        parameters: dict[str, JsonValue] | None = None,
    ) -> UUID:
        existing = await self._session.scalar(
            select(AgentVersionModel).where(
                AgentVersionModel.agent_name == entry.agent_name,
                AgentVersionModel.version == entry.version,
            )
        )
        parameter_json = _json_object(parameters or {})
        dependencies_json = _json_object(entry.data_dependencies)
        if existing is not None:
            if (
                existing.maturity != entry.maturity.value
                or existing.implementation_hash != entry.implementation_hash
                or existing.parameter_json != parameter_json
                or existing.data_dependencies_json != dependencies_json
                or existing.enabled != entry.enabled
            ):
                raise VersionIdentityConflict(
                    f"agent version conflict: {entry.agent_name}/{entry.version}"
                )
            return existing.id
        model = AgentVersionModel(
            agent_name=entry.agent_name,
            version=entry.version,
            maturity=entry.maturity.value,
            implementation_hash=entry.implementation_hash,
            parameter_json=parameter_json,
            data_dependencies_json=dependencies_json,
            enabled=entry.enabled,
        )
        self._session.add(model)
        await self._session.flush()
        return model.id

    async def create(self, manifest: ExperimentManifest) -> None:
        existing = await self._session.get(ExperimentModel, manifest.experiment_id)
        if existing is not None:
            if existing.manifest_hash != manifest.manifest_hash:
                raise ImmutableManifestError("experiment ID already has a different manifest")
            return
        for entry in manifest.agent_versions:
            await self.register_agent_version(entry)
        manifest_json = _json_object(manifest.to_dict())
        model = ExperimentModel(
            id=manifest.experiment_id,
            name=manifest.name,
            mode=manifest.mode.value,
            status=ExperimentStatus.CREATED.value,
            initial_capital=manifest.initial_capital,
            currency=manifest.currency,
            created_at=manifest.created_at,
            git_sha=manifest.git_sha,
            worktree_hash=manifest.worktree_hash,
            lock_hash=manifest.lock_hash,
            schema_revision=manifest.schema_revision,
            config_json=_json_object(manifest.configuration),
            config_hash=manifest.config_hash,
            manifest_json=manifest_json,
            manifest_hash=manifest.manifest_hash,
            manifest_schema_version=manifest.manifest_schema_version,
        )
        self._session.add(model)
        await self._session.flush()
        event_payload = freeze_json(
            {
                "status": ExperimentStatus.CREATED.value,
                "config_hash": manifest.config_hash,
                "manifest_hash": manifest.manifest_hash,
                "manifest": manifest_json,
            }
        )
        if not isinstance(event_payload, Mapping):
            raise TypeError("experiment event payload must be an object")
        await self._events.append(
            manifest.experiment_id,
            "experiment",
            0,
            (
                NewDomainEvent(
                    aggregate_id=manifest.experiment_id,
                    aggregate_type="experiment",
                    event_type="experiment.created",
                    payload=event_payload,
                    metadata={"manifest_schema_version": manifest.manifest_schema_version},
                    occurred_at=manifest.created_at,
                ),
            ),
        )

    async def transition(
        self,
        manifest: ExperimentManifest,
        transition: ExperimentTransition,
    ) -> None:
        model = await self._session.scalar(
            select(ExperimentModel)
            .where(ExperimentModel.id == manifest.experiment_id)
            .with_for_update()
        )
        if model is None:
            raise LookupError(f"experiment not found: {manifest.experiment_id}")
        if model.manifest_hash != manifest.manifest_hash:
            raise ImmutableManifestError(
                "stored manifest identity does not match transition manifest"
            )
        previous_status = str(transition.events[0].payload["previous_status"])
        if model.status != previous_status:
            raise RuntimeError(
                f"experiment projection status is {model.status}, expected {previous_status}"
            )
        model.status = transition.status.value
        occurred_at = transition.events[0].occurred_at
        if transition.status is ExperimentStatus.RUNNING and model.started_at is None:
            model.started_at = occurred_at
        if transition.status in {
            ExperimentStatus.STOPPED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
        }:
            model.ended_at = occurred_at
        if transition.status is ExperimentStatus.FAILED:
            reason = transition.events[0].payload.get("failure_reason")
            model.failure_reason = str(reason) if reason is not None else None
        await self._events.append(
            manifest.experiment_id,
            "experiment",
            transition.expected_version,
            transition.events,
        )

    async def get_manifest(self, experiment_id: UUID) -> ExperimentManifest:
        model = await self._session.get(ExperimentModel, experiment_id)
        if model is None:
            raise LookupError(f"experiment not found: {experiment_id}")
        return ExperimentManifest.from_dict(cast(dict[str, object], model.manifest_json))

    async def get_status(self, experiment_id: UUID) -> ExperimentStatus:
        model = await self._session.get(ExperimentModel, experiment_id)
        if model is None:
            raise LookupError(f"experiment not found: {experiment_id}")
        return ExperimentStatus(model.status)

    async def ensure_running(
        self,
        manifest: ExperimentManifest,
        *,
        started_at: datetime,
    ) -> bool:
        model = await self._session.scalar(
            select(ExperimentModel)
            .where(ExperimentModel.id == manifest.experiment_id)
            .with_for_update()
        )
        if model is None:
            raise LookupError(f"experiment not found: {manifest.experiment_id}")
        if model.manifest_hash != manifest.manifest_hash:
            raise ImmutableManifestError("stored manifest identity does not match worker manifest")
        status = ExperimentStatus(model.status)
        if status is ExperimentStatus.RUNNING:
            return False
        if status is not ExperimentStatus.CREATED:
            raise RuntimeError(f"paper worker cannot start experiment from {status.value}")
        version = await self._events.stream_version(manifest.experiment_id, "experiment")
        transition = ExperimentLifecycle(
            manifest,
            status,
            version,
            now=lambda: started_at,
        ).start()
        await self.transition(manifest, transition)
        return True

    async def get_agent_version_ids(
        self,
        manifest: ExperimentManifest,
    ) -> dict[str, UUID]:
        rows = (
            await self._session.execute(
                select(
                    AgentVersionModel.agent_name,
                    AgentVersionModel.version,
                    AgentVersionModel.id,
                ).where(
                    AgentVersionModel.agent_name.in_(ALL_AGENTS),
                )
            )
        ).all()
        by_identity = {(name, version): version_id for name, version, version_id in rows}
        result: dict[str, UUID] = {}
        for entry in manifest.agent_versions:
            version_id = by_identity.get((entry.agent_name, entry.version))
            if version_id is None:
                raise LookupError(
                    f"agent version is not registered: {entry.agent_name}/{entry.version}"
                )
            result[entry.agent_name] = version_id
        if tuple(result) != ALL_AGENTS:
            raise RuntimeError("registered agent identities differ from manifest ordering")
        return result

    async def fail_active(
        self,
        manifest: ExperimentManifest,
        *,
        reason: str,
        failed_at: datetime,
    ) -> bool:
        """Fail a running/paused experiment idempotently inside the current transaction."""

        model = await self._session.scalar(
            select(ExperimentModel)
            .where(ExperimentModel.id == manifest.experiment_id)
            .with_for_update()
        )
        if model is None:
            raise LookupError(f"experiment not found: {manifest.experiment_id}")
        if model.manifest_hash != manifest.manifest_hash:
            raise ImmutableManifestError("stored manifest identity does not match halt manifest")
        if model.status == ExperimentStatus.FAILED.value:
            if model.failure_reason != reason:
                raise RuntimeError("experiment is already failed for a different reason")
            return False
        status = ExperimentStatus(model.status)
        if status not in {ExperimentStatus.RUNNING, ExperimentStatus.PAUSED}:
            raise RuntimeError(f"cannot persistently halt experiment from {status.value}")
        version = await self._events.stream_version(manifest.experiment_id, "experiment")
        transition = ExperimentLifecycle(
            manifest,
            status,
            version,
            now=lambda: failed_at,
        ).fail(reason)
        await self.transition(manifest, transition)
        return True
