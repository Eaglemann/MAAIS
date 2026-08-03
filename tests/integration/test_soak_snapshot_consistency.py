from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from maais.api.queries import MissionControlQueryService
from maais.db.unit_of_work import UnitOfWork
from maais.market_data.integrity.state_machine import IntegrityPolicy, MarketIntegrityStateMachine
from maais.operations.soak_readiness import _database_soak_state
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.unit.decisions.test_bundle import _valid_bundle
from tests.unit.market_data.test_integrity_state_machine import _context, _frame

pytestmark = pytest.mark.integration


async def test_soak_snapshot_keeps_overview_and_decision_rows_consistent(
    monkeypatch: pytest.MonkeyPatch,
    uow_factory: UnitOfWork,
    test_database_url: str,
) -> None:
    manifest, first_bundle = await _prepare_bundle(uow_factory)
    assessment = MarketIntegrityStateMachine(IntegrityPolicy.official()).evaluate(
        _context(_frame())
    )
    first_bundle = replace(
        first_bundle,
        market_frame=replace(first_bundle.market_frame, id=assessment.frame_id),
        cycle=replace(first_bundle.cycle, market_frame_id=assessment.frame_id),
    )
    first_bundle.validate()
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(first_bundle)
        await uow.market_data.record_quality(
            assessment,
            evaluated_at=first_bundle.market_frame.observed_at,
            required_checks=IntegrityPolicy.official().required_checks,
        )

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
    snapshot_task = asyncio.create_task(_database_soak_state(test_database_url, manifest))

    await asyncio.wait_for(overview_complete.wait(), timeout=5)
    try:
        async with uow_factory.begin() as uow:
            await uow.decisions.record_bundle(second_bundle)
    finally:
        concurrent_write_complete.set()

    snapshot = await snapshot_task
    assert len(snapshot) == 6
    (
        overview,
        _ledger,
        decision_times,
        _quality_failures,
        _unsafe_admissions,
        decision_metadata,
    ) = snapshot

    decisions = overview["decisions"]
    assert isinstance(decisions, dict)
    assert decisions["total"] == 1
    assert sum(len(values) for values in decision_times.values()) == 1
    assert decision_metadata == {
        "passed": True,
        "decision_cycles": 1,
        "market_frames": 1,
        "decision_summaries": 1,
        "agent_rows": 8,
        "expected_agent_rows": 8,
        "quality_rows": 18,
        "expected_quality_rows": 18,
        "gate_cycles": 1,
        "invalid_cycle_rows": 0,
        "invalid_frame_rows": 0,
        "missing_summary_rows": 0,
        "invalid_summary_rows": 0,
        "incomplete_agent_cycles": 0,
        "invalid_agent_rows": 0,
        "invalid_agent_versions": 0,
        "incomplete_quality_cycles": 0,
        "missing_gate_cycles": 0,
        "invalid_gate_cycles": 0,
    }
