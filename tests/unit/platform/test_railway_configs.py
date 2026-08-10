from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RAILWAY = ROOT / "railway"
VARIABLE_RUNBOOK = ROOT / "docs" / "runbooks" / "railway-variables.md"
REGION = "europe-west4-drams3a"
EXPECTED_START_COMMANDS = {
    "web.toml": "maais cloud-web",
    "worker.toml": "maais cloud-worker",
    "operations.toml": "maais cloud-operations",
    "migrator.toml": "maais cloud-migrate --expected-revision 0022",
    "verifier.toml": "maais cloud-verifier",
}


def _config(name: str) -> dict[str, object]:
    return tomllib.loads((RAILWAY / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", tuple(EXPECTED_START_COMMANDS))
def test_every_service_uses_one_deterministic_image_and_explicit_role(name: str) -> None:
    config = _config(name)

    assert config["build"] == {
        "builder": "DOCKERFILE",
        "dockerfilePath": "Dockerfile",
    }
    deploy = config["deploy"]
    assert isinstance(deploy, dict)
    assert deploy["startCommand"] == EXPECTED_START_COMMANDS[name]
    assert deploy["multiRegionConfig"] == {REGION: {"numReplicas": 1}}
    assert deploy["restartPolicyType"] == "NEVER"
    assert deploy["overlapSeconds"] == 0
    assert deploy["drainingSeconds"] == 30
    assert "preDeployCommand" not in deploy
    assert "cronSchedule" not in deploy


def test_only_web_exposes_a_bounded_liveness_deploy_probe() -> None:
    for name in EXPECTED_START_COMMANDS:
        deploy = _config(name)["deploy"]
        assert isinstance(deploy, dict)
        if name == "web.toml":
            assert deploy["healthcheckPath"] == "/healthz/live"
            assert deploy["healthcheckTimeout"] == 120
        else:
            assert "healthcheckPath" not in deploy
            assert "healthcheckTimeout" not in deploy


def test_only_migrator_can_execute_schema_changes() -> None:
    commands = {
        name: str(_config(name)["deploy"]["startCommand"]) for name in EXPECTED_START_COMMANDS
    }

    assert "cloud-migrate" in commands["migrator.toml"]
    assert all(
        "cloud-migrate" not in command and "alembic" not in command
        for name, command in commands.items()
        if name != "migrator.toml"
    )


@pytest.mark.parametrize("name", tuple(EXPECTED_START_COMMANDS))
def test_configs_never_embed_variables_or_credentials(name: str) -> None:
    raw = (RAILWAY / name).read_text(encoding="utf-8")

    assert "[variables]" not in raw
    assert "DATABASE_URL" not in raw
    assert "SENTRY" not in raw
    assert "BINANCE" not in raw
    assert "TOKEN" not in raw
    assert "SECRET" not in raw


def test_variable_runbook_names_every_provider_boundary() -> None:
    raw = VARIABLE_RUNBOOK.read_text(encoding="utf-8")

    for heading in (
        "## Shared candidate and runtime metadata",
        "## Web service",
        "## Worker service",
        "## Operations service",
        "## Migrator service",
        "## Verifier service",
        "## Railway replica store",
        "## Canonical WORM store",
        "## Backend Sentry",
        "## Public browser Sentry",
    ):
        assert heading in raw
    for statement in (
        "BINANCE_DEMO_API_KEY",
        "BINANCE_DEMO_API_SECRET",
        "SENTRY_AUTH_TOKEN",
        "operator password itself is never stored",
        "Never paste secret values into chat",
        "GitHub Actions only",
    ):
        assert statement in raw
