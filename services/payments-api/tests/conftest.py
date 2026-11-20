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

from payments_api.config import get_settings
from payments_api.constants import EXTERNAL_FUNDING_ID
from payments_api.db import get_engine, get_sessionmaker
from payments_api.main import app

SERVICE_ROOT = Path(__file__).parents[1]


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
                "TRUNCATE ledger_entries, transfers, idempotency_keys, accounts "
                "RESTART IDENTITY CASCADE"
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
