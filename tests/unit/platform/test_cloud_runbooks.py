from __future__ import annotations

import re
from pathlib import Path

from maais.cli import build_parser
from maais.config.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
RUNBOOKS = (
    ROOT / "docs/runbooks/railway-qualification.md",
    ROOT / "docs/runbooks/railway-production-preflight.md",
    ROOT / "docs/runbooks/railway-soak.md",
    ROOT / "docs/runbooks/railway-recovery.md",
    ROOT / "docs/runbooks/railway-incidents.md",
)


def test_cloud_runbook_commands_exist_and_safety_boundaries_are_explicit() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if action.dest == "command"  # noqa: SLF001
    )
    commands = set(subparsers.choices)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in RUNBOOKS)
    documented = set(re.findall(r"uv run maais ([a-z0-9-]+)", combined))

    assert documented <= commands
    assert "paper-only" in combined
    assert "explicit operator approval" in combined
    assert "not authorization" in combined
    assert "Zero fills alone is not a failure" in combined
    assert "Never overwrite production PostgreSQL" in combined
    assert "exchange credential" in combined


def test_cloud_runbook_variable_names_are_in_settings_or_provider_contract() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in RUNBOOKS)
    documented = set(re.findall(r"`((?:MAAIS|SENTRY|VITE|RAILWAY)_[A-Z0-9_]+)`", combined))
    settings_names: set[str] = set()
    for name, field in Settings.model_fields.items():
        settings_names.add(name.upper())
        alias = field.validation_alias
        if isinstance(alias, str):
            settings_names.add(alias)
    provider_names = {
        "RAILWAY_GIT_COMMIT_SHA",
        "RAILWAY_REPLICA_ID",
        "RAILWAY_REPLICA_REGION",
    }

    assert documented <= settings_names | provider_names


def test_cloud_runbooks_do_not_instruct_secret_or_destructive_shortcuts() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in RUNBOOKS)
    forbidden = (
        "BINANCE_DEMO_API_KEY=",
        "BINANCE_DEMO_API_SECRET=",
        "SENTRY_DSN=",
        "MAAIS_OPERATOR_PASSWORD_HASH=",
        "docker login",
        "git reset --hard",
        "DROP DATABASE",
        "rm -rf",
    )

    assert not any(value in combined for value in forbidden)
