from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from maais.config.modes import RunMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    run_mode: RunMode = RunMode.REPLAY
    binance_demo_api_key: str = ""
    binance_demo_api_secret: str = ""
    database_url: str = (
        "postgresql+psycopg://maais:maais@"  # pragma: allowlist secret
        "localhost:5432/maais"
    )
    duckdb_path: str = "./data/maais.duckdb"
    kafka_bootstrap_servers: str = "localhost:9092"
    log_level: str = "INFO"
    environment: str = "development"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    mission_control_token_file: Path | None = Path("artifacts/run-state/mission-control.token")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
