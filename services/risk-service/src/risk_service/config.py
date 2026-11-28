from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings, read from environment variables (and a local .env file in development)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    model_path: Path = Path("models/risk_model.joblib")
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"


@lru_cache
def get_settings() -> Settings:
    return Settings()
