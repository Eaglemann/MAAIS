from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from maais.db.unit_of_work import UnitOfWork
from maais.orchestration.bootstrap import RuntimeBootstrapError, restore_live_paper_runtime
from tests.integration.test_operational_state_repository import NOW, _cursor, _recovery
from tests.unit.experiments.test_runtime_policy import _live_manifest

pytestmark = pytest.mark.integration


async def test_runtime_bootstrap_restores_exact_pinned_identity(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=201),
        schema_revision="0017",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)

    snapshot = await restore_live_paper_runtime(uow_factory, manifest)

    assert snapshot.manifest == manifest
    assert snapshot.policy.strategy_key == "maais_primary"
    assert snapshot.database_schema_revision == "0017"
    assert snapshot.strategy_version_id.int != 0
    assert tuple(snapshot.agent_version_ids) == tuple(
        entry.agent_name for entry in manifest.agent_versions
    )
    assert snapshot.cursors == ()
    assert snapshot.history == ()
    assert snapshot.recoveries == ()


async def test_runtime_bootstrap_refuses_manifest_or_schema_drift(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=202),
        schema_revision="0014",
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)

    with pytest.raises(RuntimeBootstrapError, match="schema revision"):
        await restore_live_paper_runtime(uow_factory, manifest)

    drifted = replace(manifest, name="different immutable run")
    with pytest.raises(RuntimeBootstrapError, match="manifest"):
        await restore_live_paper_runtime(uow_factory, drifted)


async def test_runtime_bootstrap_refuses_failed_gap_recovery(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _live_manifest(
        experiment_id=UUID(int=203),
        schema_revision="0017",
    )
    recovery = _recovery(manifest.experiment_id).fail(
        "backfill exhausted",
        NOW + timedelta(seconds=2),
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
        await uow.market_data.record_cursor(_cursor(manifest.experiment_id))
        await uow.market_data.record_recovery(recovery)

    with pytest.raises(RuntimeBootstrapError, match="operator review"):
        await restore_live_paper_runtime(uow_factory, manifest)
