from uuid import UUID

import pytest

from maais.api.queries import MissionControlQueryService
from maais.db.unit_of_work import UnitOfWork
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration


async def test_empty_experiment_uses_manifest_as_explicit_account_source(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(experiment_id=UUID(int=701), schema_revision="0015")
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    async with uow_factory.begin() as uow:
        overview = await MissionControlQueryService(uow.session).get_overview(
            manifest.experiment_id
        )

    assert overview.account.source == "manifest_initial_state"
    assert overview.account.equity == manifest.initial_capital
    assert overview.account.cash_balance == manifest.initial_capital
    assert overview.decisions.total == 0
    assert overview.operations.open_positions == 0
    assert overview.freshness.expected_symbols == len(manifest.symbols)
    assert overview.freshness.cursor_count == 0


async def test_decision_feed_and_drilldown_preserve_complete_lineage(
    uow_factory: UnitOfWork,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
    async with uow_factory.begin() as uow:
        queries = MissionControlQueryService(uow.session)
        page = await queries.list_decisions(
            manifest.experiment_id,
            symbol=bundle.cycle.symbol.lower(),
            limit=1,
        )
        detail = await queries.get_decision(bundle.cycle.id)
        overview = await queries.get_overview(manifest.experiment_id)

    assert not page.has_more
    assert len(page.items) == 1
    assert page.items[0].id == bundle.cycle.id
    assert overview.decisions.total == 1
    assert len(detail.agents) == 8
    assert {agent["agent_name"] for agent in detail.agents} == {
        evaluation.agent_name for evaluation in bundle.agents
    }
    assert len(detail.gates) == len(bundle.gates)
    assert detail.market_frame["content_hash"] == bundle.market_frame.content_hash
    assert detail.lineage_hashes["experiment_manifest"] == manifest.manifest_hash
    assert detail.lineage_hashes["decision_cycle"] == bundle.bundle_hash
    assert tuple(event.global_position for event in detail.timeline) == tuple(
        sorted(event.global_position for event in detail.timeline)
    )


async def test_query_limits_fail_closed(uow_factory: UnitOfWork) -> None:
    async with uow_factory.begin() as uow:
        queries = MissionControlQueryService(uow.session)
        with pytest.raises(ValueError, match="between 1 and 200"):
            await queries.list_experiments(limit=0)
        with pytest.raises(ValueError, match="between 1 and 500"):
            await queries.list_decisions(UUID(int=1), limit=501)
