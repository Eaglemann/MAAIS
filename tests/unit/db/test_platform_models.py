from pathlib import Path

from tests.integration.test_platform_schema import (
    test_platform_model_contract_is_exact as _assert_platform_model_contract,
)


def test_platform_model_contract_without_database() -> None:
    _assert_platform_model_contract()


def test_ci_checks_metadata_and_migration_reversibility() -> None:
    workflow = (Path(__file__).parents[3] / ".github" / "workflows" / "ci.yml").read_text()

    assert "uv run alembic check" in workflow
    assert "uv run alembic downgrade -1" in workflow
    assert "grep -Fx 0021" in workflow
