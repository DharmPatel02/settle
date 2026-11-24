from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings, read from environment variables (and a local .env file in development)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn
    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")
    transfer_rate_capacity: int = Field(default=10, gt=0)
    transfer_rate_refill_per_second: float = Field(default=1.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    # Loaded on first use, not at import time, so code that never touches
    # the database (like the liveness test) does not need DATABASE_URL.
    return Settings()
