from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from payments_api.models import OutboxEvent, Transfer


def create_account(client: TestClient, owner: str, currency: str = "USD") -> str:
    response = client.post("/v1/accounts", json={"owner_name": owner, "currency": currency})
    assert response.status_code == 201
    return response.json()["id"]


def deposit(client: TestClient, account_id: str, amount: int, key: str) -> None:
    response = client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": key},
        json={"account_id": account_id, "amount_minor": amount, "currency": "USD"},
    )
    assert response.status_code == 201


def transfer_events(db_session: Session) -> list[OutboxEvent]:
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.aggregate_type == "transfer")
            .order_by(OutboxEvent.created_at)
        )
    )


def test_completed_transfer_writes_one_event_even_when_replayed(
    client: TestClient, db_session: Session
):
    alice = create_account(client, "Alice")
    bob = create_account(client, "Bob")
    deposit(client, alice, 500, "fund-alice")
    body = {"from_account_id": alice, "to_account_id": bob, "amount_minor": 125, "currency": "USD"}

    first = client.post("/v1/transfers", headers={"Idempotency-Key": "t-1"}, json=body)
    replay = client.post("/v1/transfers", headers={"Idempotency-Key": "t-1"}, json=body)

    assert first.status_code == replay.status_code == 201
    events = [e for e in transfer_events(db_session) if str(e.aggregate_id) == first.json()["id"]]
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "TransferCompleted"
    assert event.partition_key == alice
    assert event.payload["amount_minor"] == 125
    assert event.payload["status"] == "COMPLETED"
    assert event.published_at is None


def test_insufficient_funds_records_a_rejected_transfer_and_event(
    client: TestClient, db_session: Session
):
    alice = create_account(client, "Alice")
    bob = create_account(client, "Bob")
    body = {"from_account_id": alice, "to_account_id": bob, "amount_minor": 1, "currency": "USD"}

    response = client.post("/v1/transfers", headers={"Idempotency-Key": "broke"}, json=body)

    assert response.status_code == 422
    transfer_id = response.json()["transfer_id"]
    transfer = db_session.get(Transfer, transfer_id)
    assert transfer is not None
    assert transfer.status == "REJECTED"
    assert transfer.failure_reason == "INSUFFICIENT_FUNDS"
    assert client.get(f"/v1/transfers/{transfer_id}").json()["status"] == "REJECTED"
    events = transfer_events(db_session)
    assert [(e.event_type, str(e.aggregate_id)) for e in events] == [
        ("TransferRejected", transfer_id)
    ]


def test_requests_for_unknown_accounts_write_no_event(client: TestClient, db_session: Session):
    alice = create_account(client, "Alice")
    body = {
        "from_account_id": alice,
        "to_account_id": "00000000-0000-0000-0000-00000000dead",
        "amount_minor": 1,
        "currency": "USD",
    }

    response = client.post("/v1/transfers", headers={"Idempotency-Key": "ghost"}, json=body)

    assert response.status_code == 404
    assert transfer_events(db_session) == []
