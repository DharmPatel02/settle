from functools import lru_cache

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings, read from environment variables (and a local .env file in development)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn
    admin_token: str = Field(min_length=16)
    kafka_bootstrap_servers: str = "localhost:9092"
    transfer_events_topic: str = "transfers.events"
    dead_letter_topic: str = "transfers.events.dlq"
    consumer_group_id: str = "webhook-service"

    delivery_poll_interval_seconds: float = Field(default=0.5, gt=0)
    delivery_batch_size: int = Field(default=20, gt=0)
    delivery_timeout_seconds: float = Field(default=5.0, gt=0)
    # A claimed delivery becomes due again after this long if the worker dies mid-send.
    delivery_lease_seconds: float = Field(default=30.0, gt=0)
    retry_base_seconds: float = Field(default=1.0, gt=0)
    retry_cap_seconds: float = Field(default=3600.0, gt=0)
    max_attempts: int = Field(default=8, gt=0)

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    consumer_metrics_port: int = 9103
    worker_metrics_port: int = 9104


@lru_cache
def get_settings() -> Settings:
    return Settings()
