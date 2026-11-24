import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from webhook_service.config import get_settings
from webhook_service.db import get_engine, get_sessionmaker
from webhook_service.main import app

SERVICE_ROOT = Path(__file__).parents[1]
ADMIN_TOKEN = "test-admin-token-123"

# Settings are read lazily; set the token before anything asks for them.
os.environ["ADMIN_TOKEN"] = ADMIN_TOKEN


def clear_caches() -> None:
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        os.environ["DATABASE_URL"] = url
        clear_caches()
        command.upgrade(Config(str(SERVICE_ROOT / "alembic.ini")), "head")
        yield url
        get_engine().dispose()
        clear_caches()
        os.environ.pop("DATABASE_URL", None)


@pytest.fixture()
def db_session(database_url: str) -> Iterator[Session]:
    del database_url
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture()
def client(database_url: str) -> Iterator[TestClient]:
    del database_url
    with TestClient(app, headers={"X-Admin-Token": ADMIN_TOKEN}) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest) -> Iterator[None]:
    if "database_url" not in request.fixturenames:
        yield
        return
    with get_engine().begin() as connection:
        connection.execute(
            text("TRUNCATE webhook_deliveries, webhook_subscriptions, processed_events CASCADE")
        )
    yield
