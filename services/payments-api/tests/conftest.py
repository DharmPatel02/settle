import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from payments_api.config import get_settings
from payments_api.constants import EXTERNAL_FUNDING_ID
from payments_api.db import get_engine, get_sessionmaker
from payments_api.main import app
from payments_api.rate_limit import RateLimitDecision, get_transfer_limiter
from payments_api.security import hash_api_key

SERVICE_ROOT = Path(__file__).parents[1]
TEST_API_KEY = "test-api-key"
TEST_CLIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000100")


class AlwaysAllowRateLimiter:
    def consume(self, client_id: str) -> RateLimitDecision:
        del client_id
        return RateLimitDecision(allowed=True, retry_after_seconds=0)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()

        config = Config(str(SERVICE_ROOT / "alembic.ini"))
        command.upgrade(config, "head")
        yield url

        get_sessionmaker.cache_clear()
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
        os.environ.pop("DATABASE_URL", None)


@pytest.fixture()
def db_session(database_url: str) -> Iterator[Session]:
    del database_url
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture()
def client(database_url: str) -> Iterator[TestClient]:
    del database_url
    app.dependency_overrides[get_transfer_limiter] = lambda: AlwaysAllowRateLimiter()
    try:
        with TestClient(app, headers={"X-API-Key": TEST_API_KEY}) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_transfer_limiter, None)


@pytest.fixture()
def unauthenticated_client(database_url: str) -> Iterator[TestClient]:
    del database_url
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest) -> Iterator[None]:
    if "database_url" not in request.fixturenames:
        yield
        return

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE outbox_events, ledger_entries, transfers, idempotency_keys, api_clients, "
                "accounts RESTART IDENTITY CASCADE"
            )
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(id, owner_name, currency, balance_minor, allow_negative) "
                "VALUES (:id, 'EXTERNAL_FUNDING', 'USD', 0, true)"
            ),
            {"id": EXTERNAL_FUNDING_ID},
        )
        connection.execute(
            text(
                "INSERT INTO api_clients (id, name, key_hash, is_admin) "
                "VALUES (:id, 'Test client', :key_hash, false)"
            ),
            {"id": TEST_CLIENT_ID, "key_hash": hash_api_key(TEST_API_KEY)},
        )

    yield

    with engine.connect() as connection:
        ledger_sum = connection.scalar(
            text("SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries")
        )
        mismatches = connection.scalar(
            text(
                "SELECT COUNT(*) FROM accounts a "
                "WHERE a.balance_minor <> COALESCE(("
                "SELECT SUM(le.amount_minor) FROM ledger_entries le WHERE le.account_id = a.id"
                "), 0)"
            )
        )
    assert ledger_sum == 0
    assert mismatches == 0
