from functools import lru_cache

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables (and a local .env file in development)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn


@lru_cache
def get_settings() -> Settings:
    # Loaded on first use, not at import time, so code that never touches
    # the database (like the liveness test) does not need DATABASE_URL.
    return Settings()
