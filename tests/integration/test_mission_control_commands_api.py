from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from maais.api.app import create_app
from maais.db.models.operations import OperatorCommandModel
from maais.db.unit_of_work import UnitOfWork
from tests.unit.experiments.test_manifest import _manifest

pytestmark = pytest.mark.integration

EXPERIMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
CONTROL_TOKEN = "local-test-token-0123456789abcdef"


def _request_body() -> dict[str, object]:
    return {
        "command_type": "emergency_halt",
        "idempotency_key": "33333333-3333-4333-8333-333333333333",
        "reason": "operator observed abnormal behavior",
        "payload": {"source": "mission_control"},
        "confirmation": "CONFIRM EMERGENCY_HALT",
    }


async def _prepare_experiment(uow_factory: UnitOfWork) -> None:
    async with uow_factory.begin() as uow:
        await uow.experiments.create(_manifest(experiment_id=EXPERIMENT_ID, schema_revision="0017"))


async def test_command_endpoint_rejects_missing_and_wrong_bearer_without_writing(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_experiment(uow_factory)
    application = create_app(
        uow_factory._session_factory,
        control_token=CONTROL_TOKEN,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        missing = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
        )
        wrong = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
            headers={"Authorization": "Bearer wrong-token"},
        )
    async with uow_factory.begin() as uow:
        command_count = await uow.session.scalar(
            select(func.count()).select_from(OperatorCommandModel)
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert command_count == 0


async def test_authorized_command_is_queued_once_and_returns_auditable_identity(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_experiment(uow_factory)
    application = create_app(
        uow_factory._session_factory,
        control_token=CONTROL_TOKEN,
    )
    headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        created = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
            headers=headers,
        )
        repeated = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
            headers=headers,
        )
    async with uow_factory.begin() as uow:
        commands = (await uow.session.scalars(select(OperatorCommandModel))).all()

    assert created.status_code == 202
    assert repeated.status_code == 202
    assert created.json() == repeated.json()
    assert created.json()["experiment_id"] == str(EXPERIMENT_ID)
    assert created.json()["status"] == "requested"
    assert created.json()["actor"] == "local_operator"
    assert created.json()["operator_confirmed"] is True
    assert len(created.json()["request_hash"]) == 64
    assert len(commands) == 1
    assert commands[0].request_hash == created.json()["request_hash"]


async def test_command_endpoint_loads_the_private_runtime_token_file(
    uow_factory: UnitOfWork,
    tmp_path: Path,
) -> None:
    await _prepare_experiment(uow_factory)
    token = "a1" * 32
    token_file = tmp_path / "mission-control.token"
    token_file.write_text(f"{token}\n", encoding="ascii")
    token_file.chmod(0o600)
    application = create_app(
        uow_factory._session_factory,
        control_token_file=token_file,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 202


async def test_idempotency_key_cannot_alias_a_different_control_request(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_experiment(uow_factory)
    application = create_app(
        uow_factory._session_factory,
        control_token=CONTROL_TOKEN,
    )
    headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    changed = _request_body()
    changed["reason"] = "a materially different operator reason"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        first = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
            headers=headers,
        )
        conflict = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=changed,
            headers=headers,
        )
    async with uow_factory.begin() as uow:
        command_count = await uow.session.scalar(
            select(func.count()).select_from(OperatorCommandModel)
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert "different operator request" in conflict.json()["detail"]
    assert command_count == 1


async def test_command_read_endpoints_expose_worker_status_and_terminal_result(
    uow_factory: UnitOfWork,
) -> None:
    await _prepare_experiment(uow_factory)
    application = create_app(
        uow_factory._session_factory,
        control_token=CONTROL_TOKEN,
    )
    headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        queued = await client.post(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            json=_request_body(),
            headers=headers,
        )
    command_id = UUID(queued.json()["command_id"])
    requested_at = datetime.fromisoformat(queued.json()["requested_at"])
    worker_id = "paper_worker:66666666-6666-4666-8666-666666666666"
    async with uow_factory.begin() as uow:
        claimed = await uow.commands.claim_next(
            EXPERIMENT_ID,
            worker_id=worker_id,
            accepted_at=requested_at + timedelta(seconds=1),
        )
        assert claimed is not None
        await uow.commands.complete(
            command_id,
            worker_id=worker_id,
            completed_at=requested_at + timedelta(seconds=2),
            result={"kill_switch_active": True, "experiment_status": "paused"},
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        listing = await client.get(
            f"/api/v1/experiments/{EXPERIMENT_ID}/commands",
            params={"status": "completed"},
        )
        detail = await client.get(f"/api/v1/commands/{command_id}")

    assert listing.status_code == 200
    assert listing.json()["items"] == [detail.json()]
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"
    assert detail.json()["accepted_by"] == worker_id
    assert detail.json()["result"] == {
        "kill_switch_active": True,
        "experiment_status": "paused",
    }
