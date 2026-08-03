import csv
import io
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from maais.api.app import create_app
from maais.db.unit_of_work import UnitOfWork
from maais.domain.enums import Direction
from maais.execution.paper.authorization import ExecutionAuthorizer
from maais.execution.paper.broker import MarketExitCommand, PaperBroker
from maais.execution.paper.clock import DeterministicClock
from maais.execution.paper.fills import MarketFillEngine
from maais.execution.paper.market import BookLevel, BookSnapshot
from maais.market_data.integrity.state_machine import IntegrityPolicy
from maais.orchestration.results import OrchestrationDisposition
from tests.integration.test_decision_lineage import _prepare_bundle
from tests.integration.test_mission_control_queries import _reidentify_bundle
from tests.integration.test_orchestration_repository import _command_in_database
from tests.integration.test_paper_execution_repository import _record
from tests.unit.orchestration.test_service import (
    _execution_service,
    _FeatureComputer,
    _features,
)

pytestmark = pytest.mark.integration


async def test_read_only_api_exposes_overview_feed_and_complete_decision(
    uow_factory: UnitOfWork,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
    application = create_app(uow_factory._session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        health = await client.get("/api/v1/health")
        experiments = await client.get("/api/v1/experiments")
        overview = await client.get(f"/api/v1/experiments/{manifest.experiment_id}/overview")
        decisions = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions",
            params={"symbol": "btcusdt"},
        )
        trades = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/trades",
            params={"symbol": "btcusdt"},
        )
        detail = await client.get(f"/api/v1/decisions/{bundle.cycle.id}")

    assert health.status_code == 200
    assert health.json()["database_transaction"] == "read only"
    assert health.json()["schema_revision"] == "0017"
    assert health.headers["cache-control"] == "no-store"
    assert experiments.status_code == 200
    assert experiments.json()[0]["experiment"]["manifest_hash"] == manifest.manifest_hash
    assert overview.status_code == 200
    assert overview.json()["account"]["source"] == "manifest_initial_state"
    assert overview.json()["experiment"]["model_assumptions"] == {
        "model_status": "frozen_paper_model",
        "leverage": 1,
        "maintenance_margin_model": "fixed_fraction_of_gross_notional",
        "maintenance_margin_rate": "0.005",
        "liquidation_price_model": "not_modeled",
        "exchange_liquidation_parity": False,
        "limitations": ["exchange_liquidation_behavior_not_modeled"],
    }
    assert decisions.status_code == 200
    assert decisions.json()["items"][0]["id"] == str(bundle.cycle.id)
    assert trades.status_code == 200
    assert trades.json()["items"][0]["decision_cycle_id"] == str(bundle.cycle.id)
    assert detail.status_code == 200
    assert len(detail.json()["agents"]) == 8
    assert detail.json()["lineage_hashes"]["decision_cycle"] == bundle.bundle_hash


async def test_api_returns_404_for_unknown_authoritative_records(
    uow_factory: UnitOfWork,
) -> None:
    application = create_app(uow_factory._session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        overview = await client.get(f"/api/v1/experiments/{UUID(int=999)}/overview")
        decisions = await client.get(f"/api/v1/experiments/{UUID(int=999)}/decisions")
        detail = await client.get(f"/api/v1/decisions/{UUID(int=999)}")

    assert overview.status_code == 404
    assert decisions.status_code == 404
    assert detail.status_code == 404


async def test_api_rejects_partial_decision_cursor(uow_factory: UnitOfWork) -> None:
    manifest, _bundle = await _prepare_bundle(uow_factory)
    application = create_app(uow_factory._session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions",
            params={"before_id": str(UUID(int=1))},
        )

    assert response.status_code == 422


async def test_decision_feed_filters_by_direction(uow_factory: UnitOfWork) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        matching = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions",
            params={"direction": "long"},
        )
        excluded = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions",
            params={"direction": "short"},
        )

    assert matching.status_code == 200
    assert [item["id"] for item in matching.json()["items"]] == [str(bundle.cycle.id)]
    assert excluded.status_code == 200
    assert excluded.json()["items"] == []


async def test_decision_feed_enforces_complete_audit_filters(
    uow_factory: UnitOfWork,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
    application = create_app(uow_factory._session_factory)
    base_url = f"/api/v1/experiments/{manifest.experiment_id}/decisions"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        matching = await client.get(
            base_url,
            params={
                "from_at": (bundle.cycle.cycle_at - timedelta(seconds=1)).isoformat(),
                "to_at": (bundle.cycle.cycle_at + timedelta(seconds=1)).isoformat(),
                "regime": bundle.cycle.regime,
                "strategy_version_id": str(bundle.cycle.strategy_version_id),
                "gate_type": "ev",
                "gate_passed": "true",
                "agent_name": bundle.agents[0].agent_name,
                "agent_direction": "long",
                "proposal_status": "approved",
                "outcome": "approved",
            },
        )
        exclusions = {
            "from_at": {"from_at": (bundle.cycle.cycle_at + timedelta(seconds=1)).isoformat()},
            "to_at": {"to_at": (bundle.cycle.cycle_at - timedelta(seconds=1)).isoformat()},
            "regime": {"regime": "ranging"},
            "strategy": {"strategy_version_id": str(UUID(int=999))},
            "gate_type": {"gate_type": "monitoring"},
            "gate_passed": {"gate_type": "ev", "gate_passed": "false"},
            "agent_name": {"agent_name": "not-a-registered-agent"},
            "agent_direction": {
                "agent_name": bundle.agents[0].agent_name,
                "agent_direction": "short",
            },
            "proposal_status": {"proposal_status": "rejected"},
            "order_status": {"order_status": "filled"},
            "outcome": {"outcome": "neutral"},
        }
        excluded = {
            name: await client.get(base_url, params=params) for name, params in exclusions.items()
        }

    assert matching.status_code == 200
    assert [item["id"] for item in matching.json()["items"]] == [str(bundle.cycle.id)]
    assert matching.json()["items"][0]["outcome"] == "approved"
    assert matching.json()["items"][0]["strategy_version_id"] == str(
        bundle.cycle.strategy_version_id
    )
    for name, response in excluded.items():
        assert response.status_code == 200, name
        assert response.json()["items"] == [], name


async def test_decision_feed_rejects_inverted_time_window(uow_factory: UnitOfWork) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions",
            params={
                "from_at": (bundle.cycle.cycle_at + timedelta(seconds=1)).isoformat(),
                "to_at": bundle.cycle.cycle_at.isoformat(),
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "decision from_at must not be after to_at"


async def test_decision_exports_preserve_filters_and_complete_lineage(
    uow_factory: UnitOfWork,
) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    async with uow_factory.begin() as uow:
        await uow.decisions.record_bundle(bundle)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        matching_csv = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions/export.csv",
            params={"direction": "long"},
        )
        excluded_csv = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions/export.csv",
            params={"direction": "short"},
        )
        bundle_json = await client.get(f"/api/v1/decisions/{bundle.cycle.id}/export.json")

    assert matching_csv.status_code == 200
    assert matching_csv.headers["content-type"].startswith("text/csv")
    assert matching_csv.headers["content-disposition"].startswith("attachment;")
    matching_rows = list(csv.DictReader(io.StringIO(matching_csv.text)))
    assert len(matching_rows) == 1
    assert matching_rows[0]["decision_id"] == str(bundle.cycle.id)
    assert matching_rows[0]["strategy_version_id"] == str(bundle.cycle.strategy_version_id)
    assert matching_rows[0]["outcome"] == "approved"
    assert list(csv.DictReader(io.StringIO(excluded_csv.text))) == []

    assert bundle_json.status_code == 200
    assert bundle_json.headers["content-type"] == "application/json"
    assert bundle_json.headers["content-disposition"].startswith("attachment;")
    payload = bundle_json.json()
    assert payload["decision"]["id"] == str(bundle.cycle.id)
    assert len(payload["agents"]) == 8
    assert len(payload["gates"]) == len(bundle.gates)
    assert payload["lineage_hashes"]["decision_cycle"] == bundle.bundle_hash
    assert payload["timeline"]


async def test_trade_ledger_enforces_execution_filters(uow_factory: UnitOfWork) -> None:
    execution = await _record(uow_factory)
    assert execution.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(execution)
    application = create_app(uow_factory._session_factory)
    experiment_id = execution.account.experiment_id
    base_url = f"/api/v1/experiments/{experiment_id}/trades"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        matching = await client.get(
            base_url,
            params={
                "from_at": (execution.order.created_at - timedelta(seconds=1)).isoformat(),
                "to_at": (execution.order.created_at + timedelta(seconds=1)).isoformat(),
                "direction": "long",
                "regime": "trending",
                "proposal_status": "approved",
                "decision_disposition": "approved",
                "order_status": "filled",
                "outcome": "filled",
            },
        )
        exclusions = {
            "from_at": {"from_at": (execution.order.created_at + timedelta(seconds=1)).isoformat()},
            "to_at": {"to_at": (execution.order.created_at - timedelta(seconds=1)).isoformat()},
            "direction": {"direction": "short"},
            "regime": {"regime": "ranging"},
            "proposal_status": {"proposal_status": "rejected"},
            "decision_disposition": {"decision_disposition": "rejected"},
            "order_status": {"order_status": "accepted"},
            "counterfactual_status": {"counterfactual_status": "closed"},
            "outcome": {"outcome": "counterfactual"},
        }
        excluded = {
            name: await client.get(base_url, params=params) for name, params in exclusions.items()
        }

    assert matching.status_code == 200
    assert [item["proposal_id"] for item in matching.json()["items"]] == [
        str(execution.order.proposal_id)
    ]
    assert matching.json()["items"][0]["outcome"] == "filled"
    assert matching.json()["items"][0]["strategy_version_id"]
    for name, response in excluded.items():
        assert response.status_code == 200, name
        assert response.json()["items"] == [], name


async def test_trade_ledger_rejects_inverted_time_window(uow_factory: UnitOfWork) -> None:
    manifest, bundle = await _prepare_bundle(uow_factory)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/trades",
            params={
                "from_at": (bundle.cycle.cycle_at + timedelta(seconds=1)).isoformat(),
                "to_at": bundle.cycle.cycle_at.isoformat(),
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "trade from_at must not be after to_at"


async def test_trade_export_preserves_filters_and_execution_costs(
    uow_factory: UnitOfWork,
) -> None:
    execution = await _record(uow_factory)
    assert execution.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(execution)
    application = create_app(uow_factory._session_factory)
    experiment_id = execution.account.experiment_id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        matching = await client.get(
            f"/api/v1/experiments/{experiment_id}/trades/export.csv",
            params={"direction": "long", "outcome": "filled"},
        )
        excluded = await client.get(
            f"/api/v1/experiments/{experiment_id}/trades/export.csv",
            params={"direction": "short"},
        )

    assert matching.status_code == 200
    assert matching.headers["content-type"].startswith("text/csv")
    assert matching.headers["content-disposition"].startswith("attachment;")
    rows = list(csv.DictReader(io.StringIO(matching.text)))
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == str(execution.order.proposal_id)
    assert rows[0]["outcome"] == "filled"
    assert rows[0]["order_statuses"] == "filled"
    assert rows[0]["fill_count"] == "1"
    assert rows[0]["fees"] == "3.000000000000000000"
    assert rows[0]["total_slippage"] == "0.070000000000000000"
    assert list(csv.DictReader(io.StringIO(excluded.text))) == []


async def test_decision_and_trade_csv_exports_cross_the_internal_page_boundary(
    uow_factory: UnitOfWork,
) -> None:
    manifest, first = await _prepare_bundle(uow_factory)
    bundles = [first]
    for index in range(1, 501):
        bundle = _reidentify_bundle(first, f"SYM{index:03d}USDT")
        bundles.append(
            replace(
                bundle,
                market_frame=replace(
                    bundle.market_frame,
                    content_hash=f"{index:064x}",
                ),
            )
        )
    async with uow_factory.begin() as uow:
        for bundle in bundles:
            await uow.decisions.record_bundle(bundle)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        decision_response = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/decisions/export.csv"
        )
        trade_response = await client.get(
            f"/api/v1/experiments/{manifest.experiment_id}/trades/export.csv"
        )

    assert decision_response.status_code == 200
    decision_rows = list(csv.DictReader(io.StringIO(decision_response.text)))
    assert len(decision_rows) == 501
    assert {row["decision_id"] for row in decision_rows} == {
        str(bundle.cycle.id) for bundle in bundles
    }
    assert trade_response.status_code == 200
    trade_rows = list(csv.DictReader(io.StringIO(trade_response.text)))
    assert len(trade_rows) == 501
    assert {row["decision_cycle_id"] for row in trade_rows} == {
        str(bundle.cycle.id) for bundle in bundles
    }


async def test_api_serves_built_dashboard_without_weakening_api_transactions(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "index.html").write_text(
        "<!doctype html><title>Mission Control fixture</title>",
        encoding="utf-8",
    )
    application = create_app(uow_factory._session_factory, dashboard_dir=dashboard)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        page = await client.get("/")
        health = await client.get("/api/v1/health")

    assert page.status_code == 200
    assert "Mission Control fixture" in page.text
    assert health.json()["database_transaction"] == "read only"


async def test_research_lab_exposes_execution_sensitivities_outside_official_account(
    uow_factory: UnitOfWork,
) -> None:
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
    )
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.disposition is OrchestrationDisposition.EXECUTED
    async with uow_factory.begin() as uow:
        await uow.orchestration.record_outcome(
            outcome,
            integrity=command.integrity,
            required_checks=IntegrityPolicy.official().required_checks,
            evaluated_at=command.evaluated_at,
        )
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/experiments/{command.manifest.experiment_id}/research"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["official_account_inclusion"] == "excluded"
    assert payload["counterfactuals"] == []
    assert [item["scenario"] for item in payload["execution_sensitivities"]] == [
        "conservative",
        "optimistic",
        "stress",
    ]
    assert all(item["symbol"] == "BTCUSDT" for item in payload["execution_sensitivities"])
    assert all(item["decision_cycle_id"] for item in payload["execution_sensitivities"])


async def test_research_lab_exposes_reconciled_official_performance_analytics(
    uow_factory: UnitOfWork,
) -> None:
    execution = await _record(uow_factory)
    assert execution.account is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(execution)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/experiments/{execution.account.experiment_id}/research"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["analytics_as_of"] == "2026-08-02T12:00:00.003000Z"
    assert payload["equity_curve"] == [
        {
            "at": "2026-08-02T12:00:00.003000Z",
            "equity": "9997.000000000000000000",
            "drawdown": "0.000300000000000000",
        }
    ]
    assert payload["cost_waterfall"] == {
        "initial_capital": "10000.000000000000000000",
        "gross_realized_pnl": "0E-18",
        "fees": "-3.000000000000000000",
        "funding": "0E-18",
        "unrealized_pnl": "0E-18",
        "net_change": "-3.000000000000000000",
        "ending_equity": "9997.000000000000000000",
        "reconciles": True,
    }
    assert payload["performance"]["closed_trade_allocations"] == 0
    assert payload["availability"]["closed_trade_metrics"]["status"] == "unavailable"
    assert payload["benchmarks"]["flat_cash"]["ending_equity"] == ("10000.000000000000000000")


async def test_research_lab_attributes_a_closed_official_trade_from_fifo_ledger(
    uow_factory: UnitOfWork,
) -> None:
    entry = await _record(uow_factory)
    assert entry.account is not None
    assert entry.exit_plan is not None
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(entry)
    trigger_at = entry.fills[0].fill_at + timedelta(minutes=1)
    triggered = entry.exit_plan.evaluate_mark(entry.exit_plan.target_price, trigger_at)
    assert triggered.intent is not None
    book_at = trigger_at + timedelta(milliseconds=101)
    broker = PaperBroker(
        clock=DeterministicClock(lambda: trigger_at),
        authorizer=ExecutionAuthorizer(b"paper research integration key is at least 32 bytes"),
        market_fills=MarketFillEngine(timedelta(seconds=1)),
    )
    result = broker.execute_market_exit(
        MarketExitCommand(
            order_id=UUID(int=9901),
            fill_id=UUID(int=9902),
            experiment_id=entry.account.experiment_id,
            proposal_id=entry.order.proposal_id,
            client_order_id="paper-btc-research-target",
            symbol="BTCUSDT",
            decision_executable_price=entry.exit_plan.target_price,
            execution_latency=timedelta(milliseconds=100),
            created_at=trigger_at,
            expires_at=trigger_at + timedelta(seconds=30),
            taker_fee_rate=Decimal("0.0005"),
            intent=triggered.intent,
            exchange_filters=entry.exchange_filters,
        ),
        account=entry.account,
        exit_plan=triggered.plan,
        books=(
            BookSnapshot(
                event_id="research-target-depth",
                symbol="BTCUSDT",
                venue_event_at=book_at - timedelta(milliseconds=1),
                observed_at=book_at,
                sequence=991,
                bids=(BookLevel(Decimal("61000"), Decimal("1")),),
                asks=(BookLevel(Decimal("61001"), Decimal("1")),),
                mark_price=Decimal("61000.5"),
            ),
        ),
    )
    async with uow_factory.begin() as uow:
        await uow.paper_execution.record(result.record)
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/api/v1/experiments/{entry.account.experiment_id}/research")

    assert response.status_code == 200
    payload = response.json()
    assert payload["performance"]["closed_trade_allocations"] == 1
    assert Decimal(payload["performance"]["expectancy"]) == Decimal("93.95")
    symbol_result = payload["attribution"]["by_symbol"][0]
    assert symbol_result["key"] == "BTCUSDT"
    assert symbol_result["trades"] == 1
    assert symbol_result["wins"] == 1
    assert symbol_result["losses"] == 0
    assert Decimal(symbol_result["win_rate"]) == Decimal("1")
    assert Decimal(symbol_result["net_pnl_ex_funding"]) == Decimal("93.95")
    assert Decimal(symbol_result["expectancy"]) == Decimal("93.95")
    assert payload["attribution"]["by_exit_reason"][0]["key"] == "target"
    assert payload["calibration"]["consensus"]["sample_size"] == 1


async def test_research_lab_exposes_rejected_trade_counterfactual_with_lineage(
    uow_factory: UnitOfWork,
) -> None:
    command = await _command_in_database(
        uow_factory,
        quarantine=False,
        with_entry_context=True,
        kill_switch=True,
    )
    outcome = await _execution_service(_FeatureComputer(_features()), Direction.LONG).process(
        command
    )
    assert outcome.disposition is OrchestrationDisposition.REJECTED
    async with uow_factory.begin() as uow:
        await uow.orchestration.record_outcome(
            outcome,
            integrity=command.integrity,
            required_checks=IntegrityPolicy.official().required_checks,
            evaluated_at=command.evaluated_at,
        )
    application = create_app(uow_factory._session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/v1/experiments/{command.manifest.experiment_id}/research"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["official_account_inclusion"] == "excluded"
    assert payload["execution_sensitivities"] == []
    counterfactual = payload["counterfactuals"][0]
    assert counterfactual["decision_cycle_id"] == str(outcome.bundle.cycle.id)
    assert counterfactual["proposal_id"] == str(outcome.bundle.proposal.id)
    assert counterfactual["symbol"] == "BTCUSDT"
    assert counterfactual["rejection_gate"] == "monitoring"
    assert len(counterfactual["content_hash"]) == 64
