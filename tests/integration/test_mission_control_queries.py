from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from maais.api.queries import MissionControlQueryService
from maais.db.repositories.execution import PaperExecutionRecord
from maais.db.unit_of_work import UnitOfWork
from maais.decisions.bundle import DecisionBundle
from maais.domain.enums import PaperOrderSide, PaperOrderType, PositionEffect
from maais.execution.paper.orders import PaperOrder
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
    assert overview.experiment.model_assumptions.model_status == "frozen_paper_model"
    assert overview.experiment.model_assumptions.leverage == 1
    assert overview.experiment.model_assumptions.maintenance_margin_rate == Decimal("0.005")
    assert overview.experiment.model_assumptions.liquidation_price_model == "not_modeled"
    assert overview.experiment.model_assumptions.exchange_liquidation_parity is False
    assert overview.experiment.model_assumptions.limitations == (
        "exchange_liquidation_behavior_not_modeled",
    )
    assert overview.decisions.total == 0
    assert overview.operations.open_positions == 0
    assert overview.freshness.expected_symbols == len(manifest.symbols)
    assert overview.freshness.cursor_count == 0


async def test_legacy_experiment_discloses_unsupported_model_without_hiding_history(
    uow_factory: UnitOfWork,
) -> None:
    manifest = _manifest(
        experiment_id=UUID(int=702),
        schema_revision="0015",
        configuration={"risk": {"leverage": 1}, "symbols": ["BTCUSDT"]},
    )
    async with uow_factory.begin() as uow:
        await uow.experiments.create(manifest)
    async with uow_factory.begin() as uow:
        overview = await MissionControlQueryService(uow.session).get_overview(
            manifest.experiment_id
        )

    assumptions = overview.experiment.model_assumptions
    assert assumptions.model_status == "legacy_or_unsupported_policy"
    assert assumptions.leverage == 1
    assert assumptions.maintenance_margin_rate is None
    assert assumptions.liquidation_price_model is None
    assert assumptions.exchange_liquidation_parity is None
    assert assumptions.limitations == ("paper_model_policy_missing_or_not_supported",)


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


async def test_decision_outcome_remains_filled_after_a_later_canceled_order(
    uow_factory: UnitOfWork,
) -> None:
    execution = await _record(uow_factory)
    assert execution.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(execution)
    created_at = execution.order.created_at + timedelta(seconds=1)
    canceled = PaperOrder.create(
        order_id=UUID(int=402),
        experiment_id=execution.order.experiment_id,
        proposal_id=execution.order.proposal_id,
        client_order_id="paper-btc-canceled-exit",
        command_hash="d" * 64,
        symbol=execution.order.symbol,
        side=PaperOrderSide.SELL,
        order_type=PaperOrderType.MARKET,
        position_effect=PositionEffect.REDUCE,
        quantity=Decimal("0.1"),
        limit_price=None,
        reduce_only=True,
        open_quantity=Decimal("0.1"),
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=30),
    ).cancel(created_at + timedelta(milliseconds=1))
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(
            PaperExecutionRecord(
                canceled,
                execution.exchange_filters,
                (),
                None,
                None,
            )
        )

    async with uow_factory.begin() as uow:
        page = await MissionControlQueryService(uow.session).list_decisions(
            execution.order.experiment_id,
            outcome="filled",
        )

    assert len(page.items) == 1
    assert page.items[0].order_status == "canceled"
    assert page.items[0].outcome == "filled"
