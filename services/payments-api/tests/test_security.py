import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payments_api.errors import Problem
from payments_api.models import ApiClient
from payments_api.security import ApiClientPrincipal, hash_api_key, require_admin_client


def test_missing_api_key_returns_401_problem(unauthenticated_client: TestClient):
    response = unauthenticated_client.post(
        "/v1/accounts", json={"owner_name": "Alice", "currency": "USD"}
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Authentication required"


def test_invalid_api_key_returns_401_problem(unauthenticated_client: TestClient):
    response = unauthenticated_client.post(
        "/v1/accounts",
        headers={"X-API-Key": "not-valid"},
        json={"owner_name": "Alice", "currency": "USD"},
    )

    assert response.status_code == 401
    assert response.json()["title"] == "Invalid API key"


def test_database_stores_only_api_key_hash(db_session: Session):
    client = db_session.scalar(select(ApiClient))

    assert client is not None
    assert client.key_hash == hash_api_key("test-api-key")
    assert client.key_hash != "test-api-key"


def test_non_admin_client_is_rejected():
    principal = ApiClientPrincipal(id=uuid.uuid4(), name="standard", is_admin=False)

    with pytest.raises(Problem) as raised:
        require_admin_client(principal)
    assert raised.value.status == 403


def test_admin_client_is_accepted():
    principal = ApiClientPrincipal(id=uuid.uuid4(), name="admin", is_admin=True)

    assert require_admin_client(principal) == principal
