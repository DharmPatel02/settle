import uuid

from fastapi.testclient import TestClient


def test_create_and_get_account(client: TestClient):
    created = client.post("/v1/accounts", json={"owner_name": "Alice", "currency": "USD"})
    assert created.status_code == 201
    account = created.json()
    assert account["owner_name"] == "Alice"
    assert account["balance_minor"] == 0

    fetched = client.get(f"/v1/accounts/{account['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == account["id"]


def test_missing_account_returns_problem(client: TestClient):
    response = client.get(f"/v1/accounts/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Account not found"


def test_new_account_has_empty_ledger(client: TestClient):
    account = client.post("/v1/accounts", json={"owner_name": "Bob", "currency": "USD"}).json()

    response = client.get(f"/v1/accounts/{account['id']}/entries")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_invalid_account_request_returns_400_problem(client: TestClient):
    response = client.post("/v1/accounts", json={"owner_name": "", "currency": "USD"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
