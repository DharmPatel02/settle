from fastapi.testclient import TestClient


def create_account(client: TestClient, owner: str, currency: str = "USD") -> dict[str, object]:
    response = client.post("/v1/accounts", json={"owner_name": owner, "currency": currency})
    assert response.status_code == 201
    return response.json()


def deposit(client: TestClient, account_id: str, amount: int, key: str) -> dict[str, object]:
    response = client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": key},
        json={"account_id": account_id, "amount_minor": amount, "currency": "USD"},
    )
    assert response.status_code == 201
    return response.json()


def test_accounts_deposits_and_cursor_ledger(client: TestClient):
    account = create_account(client, "Alice")
    deposit(client, str(account["id"]), 500, "deposit-alice-1")

    fetched = client.get(f"/v1/accounts/{account['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["balance_minor"] == 500

    first_page = client.get(f"/v1/accounts/{account['id']}/entries", params={"limit": 1})
    assert first_page.status_code == 200
    assert [entry["amount_minor"] for entry in first_page.json()["items"]] == [500]
    assert first_page.json()["next_cursor"] is None


def test_transfer_and_idempotent_replay(client: TestClient):
    alice = create_account(client, "Alice")
    bob = create_account(client, "Bob")
    deposit(client, str(alice["id"]), 500, "deposit-alice-1")
    body = {
        "from_account_id": alice["id"],
        "to_account_id": bob["id"],
        "amount_minor": 125,
        "currency": "USD",
    }

    first = client.post("/v1/transfers", headers={"Idempotency-Key": "transfer-1"}, json=body)
    replay = client.post("/v1/transfers", headers={"Idempotency-Key": "transfer-1"}, json=body)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert client.get(f"/v1/accounts/{alice['id']}").json()["balance_minor"] == 375
    assert client.get(f"/v1/accounts/{bob['id']}").json()["balance_minor"] == 125
    entries = client.get(f"/v1/accounts/{bob['id']}/entries").json()["items"]
    assert [entry["amount_minor"] for entry in entries] == [125]
    assert client.get(f"/v1/transfers/{first.json()['id']}").status_code == 200


def test_same_key_with_different_body_is_rejected(client: TestClient):
    alice = create_account(client, "Alice")
    bob = create_account(client, "Bob")
    deposit(client, str(alice["id"]), 500, "deposit-alice-1")
    body = {
        "from_account_id": alice["id"],
        "to_account_id": bob["id"],
        "amount_minor": 125,
        "currency": "USD",
    }
    assert (
        client.post(
            "/v1/transfers", headers={"Idempotency-Key": "transfer-1"}, json=body
        ).status_code
        == 201
    )

    body["amount_minor"] = 126
    response = client.post("/v1/transfers", headers={"Idempotency-Key": "transfer-1"}, json=body)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_insufficient_funds_is_stored_and_moves_nothing(client: TestClient):
    alice = create_account(client, "Alice")
    bob = create_account(client, "Bob")
    body = {
        "from_account_id": alice["id"],
        "to_account_id": bob["id"],
        "amount_minor": 1,
        "currency": "USD",
    }

    first = client.post("/v1/transfers", headers={"Idempotency-Key": "no-money"}, json=body)
    replay = client.post("/v1/transfers", headers={"Idempotency-Key": "no-money"}, json=body)

    assert first.status_code == replay.status_code == 422
    assert first.json() == replay.json()
    assert client.get(f"/v1/accounts/{alice['id']}").json()["balance_minor"] == 0
    assert client.get(f"/v1/accounts/{bob['id']}").json()["balance_minor"] == 0
