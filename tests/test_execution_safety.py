from pathlib import Path

import pytest

from maais.config.modes import RunMode
from maais.config.settings import Settings
from maais.execution.testnet.binance_client import (
    DEMO_FUTURES_BASE_URL,
    BinanceDemoFuturesClient,
    build_authenticated_execution_client,
)


def test_authenticated_execution_source_contains_no_production_url() -> None:
    source = "\n".join(path.read_text() for path in Path("maais/execution").rglob("*.py"))
    assert "https://fapi.binance.com" not in source


def test_demo_client_is_pinned_to_demo_url() -> None:
    assert DEMO_FUTURES_BASE_URL == "https://demo-fapi.binance.com"


def test_factory_rejects_non_testnet_modes() -> None:
    settings = Settings(run_mode=RunMode.PAPER_LIVE, _env_file=None)
    with pytest.raises(ValueError, match="testnet_smoke"):
        build_authenticated_execution_client(settings)


def test_factory_requires_both_demo_credentials() -> None:
    settings = Settings(run_mode=RunMode.TESTNET_SMOKE, _env_file=None)
    with pytest.raises(ValueError, match="Demo credentials"):
        build_authenticated_execution_client(settings)


def test_factory_constructs_only_demo_client() -> None:
    settings = Settings(
        run_mode=RunMode.TESTNET_SMOKE,
        binance_demo_api_key="key",
        binance_demo_api_secret="secret",
        _env_file=None,
    )
    assert isinstance(build_authenticated_execution_client(settings), BinanceDemoFuturesClient)
