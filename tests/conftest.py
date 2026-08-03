import os

import pytest

# Override env for tests before any settings are instantiated.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session", autouse=True)
def configure_test_logging():
    from maais.core.logging import configure_logging

    configure_logging(log_level="WARNING", is_production=False)


@pytest.fixture(scope="session")
def settings():
    # Reset singleton so test env vars are picked up.
    import maais.config.settings as _s

    _s._settings = None
    return _s.get_settings()
