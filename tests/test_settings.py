import pytest
from pydantic import ValidationError

from maais.config.modes import RunMode
from maais.config.settings import Settings


def test_run_modes_are_closed() -> None:
    assert {mode.value for mode in RunMode} == {"replay", "paper_live", "testnet_smoke"}
    assert RunMode.TESTNET_SMOKE.permits_authenticated_exchange
    assert not RunMode.PAPER_LIVE.permits_authenticated_exchange


def test_settings_default_to_replay_without_credentials() -> None:
    settings = Settings(_env_file=None)
    assert settings.run_mode is RunMode.REPLAY
    assert settings.binance_demo_api_key == ""
    assert settings.binance_demo_api_secret == ""
    assert settings.maais_test_database_url == ""


def test_live_is_not_a_valid_mode() -> None:
    with pytest.raises(ValidationError):
        Settings(run_mode="live", _env_file=None)
