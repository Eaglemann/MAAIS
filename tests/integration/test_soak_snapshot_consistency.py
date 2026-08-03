from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from maais.api.queries import MissionControlQueryService
from maais.db.unit_of_work import UnitOfWork
from maais.operations.soak_readiness import _database_soak_state
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.unit.decisions.test_bundle import _valid_bundle

pytestmark = pytest.mark.integration


async def test_soak_snapshot_keeps_overview_and_decision_rows_consistent(
    monkeypatch: pytest.MonkeyPatch,
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    manifest, first_bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(first_bundle)

    agent_version_ids = {
        evaluation.agent_name: evaluation.agent_version_id for evaluation in first_bundle.agents
    }
    second_bundle = _valid_bundle(
        experiment_id=manifest.experiment_id,
        strategy_version_id=first_bundle.cycle.strategy_version_id,
        agent_version_ids=agent_version_ids,
    )
    assert second_bundle.proposal is not None
    second_bundle = replace(
        second_bundle,
        market_frame=replace(
            second_bundle.market_frame,
            symbol="ETHUSDT",
            content_hash="c" * 64,
        ),
        cycle=replace(second_bundle.cycle, symbol="ETHUSDT"),
        proposal=replace(second_bundle.proposal, symbol="ETHUSDT"),
    )
    second_bundle.validate()

    original_get_overview = MissionControlQueryService.get_overview
    overview_complete = asyncio.Event()
    concurrent_write_complete = asyncio.Event()

    async def get_overview_then_wait_for_concurrent_write(
        service: MissionControlQueryService,
        experiment_id: UUID,
    ):
        overview = await original_get_overview(service, experiment_id)
        overview_complete.set()
        await asyncio.wait_for(concurrent_write_complete.wait(), timeout=5)
        return overview

    monkeypatch.setattr(
        MissionControlQueryService,
        "get_overview",
        get_overview_then_wait_for_concurrent_write,
    )
    snapshot_task = asyncio.create_task(
        _database_soak_state(test_database_url, manifest.experiment_id)
    )

    await asyncio.wait_for(overview_complete.wait(), timeout=5)
    try:
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(second_bundle)
    finally:
        concurrent_write_complete.set()

    overview, _ledger, decision_times, _quality_failures, _unsafe_admissions = await snapshot_task

    decisions = overview["decisions"]
    assert isinstance(decisions, dict)
    assert decisions["total"] == 1
    assert sum(len(values) for values in decision_times.values()) == 1
