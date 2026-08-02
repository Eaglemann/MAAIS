from pathlib import Path
from uuid import UUID

import httpx
import pytest

from maais.api.app import create_app
from maais.db.unit_of_work import UnitOfWork
from tests.integration.test_decision_lineage import _prepare_bundle

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
        detail = await client.get(f"/api/v1/decisions/{bundle.cycle.id}")

    assert health.status_code == 200
    assert health.json()["database_transaction"] == "read only"
    assert health.json()["schema_revision"] == "0015"
    assert health.headers["cache-control"] == "no-store"
    assert experiments.status_code == 200
    assert experiments.json()[0]["experiment"]["manifest_hash"] == manifest.manifest_hash
    assert overview.status_code == 200
    assert overview.json()["account"]["source"] == "manifest_initial_state"
    assert decisions.status_code == 200
    assert decisions.json()["items"][0]["id"] == str(bundle.cycle.id)
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
