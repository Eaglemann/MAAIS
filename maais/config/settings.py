from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    binance_api_key: str = ""
    binance_api_secret: str = ""
    database_url: str = "postgresql+psycopg://localhost/maais"
    duckdb_path: str = "./data/maais.duckdb"
    kafka_bootstrap_servers: str = "localhost:9092"
    log_level: str = "INFO"
    environment: str = "development"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
