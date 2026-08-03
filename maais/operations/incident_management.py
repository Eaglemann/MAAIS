"""Explicit, audited operator transitions for paper-trading incidents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maais.config.settings import get_settings
from maais.db.unit_of_work import UnitOfWork
from maais.domain.json import to_json_data

UTC = timezone.utc


class IncidentAction(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    RESOLVE = "resolve"


async def apply_incident_action(
    unit_of_work: UnitOfWork,
    *,
    experiment_id: UUID,
    incident_id: UUID,
    action: IncidentAction,
    actor: str,
    occurred_at: datetime,
    resolution: str | None = None,
    operator_confirmed: bool = False,
) -> dict[str, object]:
    """Append one explicit incident transition inside the authoritative ledger."""
    if occurred_at.tzinfo is None or occurred_at.utcoffset() != timedelta(0):
        raise ValueError("incident action time must be UTC-aware")
    if not actor or actor != actor.strip():
        raise ValueError("incident action actor must be nonempty and trimmed")
    if action is IncidentAction.ACKNOWLEDGE:
        if resolution is not None or operator_confirmed:
            raise ValueError("acknowledgement cannot include resolution fields")
    elif action is IncidentAction.RESOLVE:
        if not resolution or resolution != resolution.strip():
            raise ValueError("incident resolution must be nonempty and trimmed")
    else:  # pragma: no cover - enum construction prevents this for typed callers
        raise ValueError(f"unsupported incident action: {action}")

    async with unit_of_work.begin() as context:
        incident = await context.incidents.get(incident_id)
        if incident.experiment_id != experiment_id:
            raise LookupError("incident does not belong to the expected experiment")
        if action is IncidentAction.ACKNOWLEDGE:
            changed = incident.acknowledge(actor, occurred_at)
        else:
            assert resolution is not None
            changed = incident.resolve(
                actor,
                resolution,
                occurred_at,
                operator_confirmed=operator_confirmed,
            )
        persisted = await context.incidents.record(changed)

    event = changed.events[-1]
    payload = {
        "action": action,
        "experiment_id": experiment_id,
        "incident_id": incident_id,
        "status": changed.status,
        "version": changed.version,
        "changed_at": changed.changed_at,
        "actor": actor,
        "resolution": changed.resolution,
        "operator_confirmed": operator_confirmed,
        "event_sequence": event.sequence,
        "event_type": event.event_type,
        "content_hash": persisted.content_hash,
    }
    normalized = to_json_data(payload)
    if not isinstance(normalized, dict):  # pragma: no cover - fixed local shape
        raise TypeError("incident action result must be a JSON object")
    return cast(dict[str, object], normalized)


async def apply_configured_incident_action(
    *,
    experiment_id: UUID,
    incident_id: UUID,
    action: IncidentAction,
    actor: str,
    resolution: str | None = None,
    operator_confirmed: bool = False,
) -> dict[str, object]:
    """Apply an operator transition against the configured local database."""
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        return await apply_incident_action(
            UnitOfWork(async_sessionmaker(engine, expire_on_commit=False)),
            experiment_id=experiment_id,
            incident_id=incident_id,
            action=action,
            actor=actor,
            resolution=resolution,
            operator_confirmed=operator_confirmed,
            occurred_at=datetime.now(UTC),
        )
    finally:
        await engine.dispose()
