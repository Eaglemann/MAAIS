from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from maais.api.queries import MissionControlQueryService
from maais.db.unit_of_work import UnitOfWork
from maais.decisions.bundle import DecisionBundle
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.integration.test_paper_execution_repository import _record
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration


def _reidentify_bundle(bundle: DecisionBundle, symbol: str) -> DecisionBundle:
    frame_id = uuid4()
    cycle_id = uuid4()
    proposal_id = uuid4()
    return DecisionBundle(
        market_frame=replace(
            bundle.market_frame,
            id=frame_id,
            symbol=symbol,
            content_hash="c" * 64,
        ),
        cycle=replace(
            bundle.cycle,
            id=cycle_id,
            market_frame_id=frame_id,
            symbol=symbol,
        ),
        agents=tuple(
            replace(agent, id=uuid4(), decision_cycle_id=cycle_id) for agent in bundle.agents
        ),
        summary=replace(bundle.summary, decision_cycle_id=cycle_id),
        gates=tuple(replace(gate, id=uuid4(), decision_cycle_id=cycle_id) for gate in bundle.gates),
        proposal=(
            replace(
                bundle.proposal,
                id=proposal_id,
                decision_cycle_id=cycle_id,
                symbol=symbol,
            )
            if bundle.proposal is not None
            else None
        ),
    )


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


async def test_decision_cursor_does_not_skip_symbols_at_same_cycle_time(
    uow_factory: UnitOfWork,
) -> None:
    manifest, first_bundle = await _prepare_bundle(uow_factory)
    second_bundle = _reidentify_bundle(first_bundle, "ETHUSDT")
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(first_bundle)
        await uow.decisions.record_bundle(second_bundle)
    async with uow_factory.begin() as uow:
        queries = MissionControlQueryService(uow.session)
        first_page = await queries.list_decisions(manifest.experiment_id, limit=1)
        second_page = await queries.list_decisions(
            manifest.experiment_id,
            before_at=first_page.next_before_at,
            before_id=first_page.next_before_id,
            limit=1,
        )

    assert first_page.has_more
    assert not second_page.has_more
    assert {first_page.items[0].id, second_page.items[0].id} == {
        first_bundle.cycle.id,
        second_bundle.cycle.id,
    }


async def test_trade_ledger_surfaces_proposal_order_fill_cost_and_decision_lineage(
    uow_factory: UnitOfWork,
) -> None:
    execution = await _record(uow_factory)
    assert execution.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(execution)
    async with uow_factory.begin() as uow:
        page = await MissionControlQueryService(uow.session).list_trades(
            execution.account.experiment_id,
            limit=10,
        )

    assert not page.has_more
    assert len(page.items) == 1
    trade = page.items[0]
    assert trade.proposal_id == execution.order.proposal_id
    assert trade.symbol == "BTCUSDT"
    assert trade.direction == "long"
    assert trade.official_order_count == 1
    assert trade.order_statuses == ("filled",)
    assert trade.fill_count == 1
    assert trade.filled_quantity == execution.fills[0].quantity
    assert trade.fees == execution.fills[0].fee
    assert trade.total_slippage == execution.fills[0].total_slippage
    assert trade.counterfactual_status is None
