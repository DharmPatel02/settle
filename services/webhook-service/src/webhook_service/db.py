from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from webhook_service.config import get_settings
from webhook_service.sql_tracing import trace_sql


@lru_cache
def get_engine() -> Engine:
    # One engine per process: it owns the connection pool shared by all requests.
    engine = create_engine(
        str(get_settings().database_url),
        # Test each pooled connection before use, so the app recovers by itself
        # after Postgres restarts instead of failing on stale connections.
        pool_pre_ping=True,
        # Fail fast instead of hanging when Postgres is unreachable.
        connect_args={"connect_timeout": 3},
    )
    trace_sql(engine)
    return engine


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with get_sessionmaker()() as session:
        yield session
