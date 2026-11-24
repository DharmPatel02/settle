import uuid

from fastapi.testclient import TestClient
from helpers import subscribe, transfer_event
from sqlalchemy.orm import Session

from webhook_service.db import get_sessionmaker
from webhook_service.events import handle_event
from webhook_service.main import app
from webhook_service.models import WebhookDelivery


def dead_delivery(db_session: Session) -> WebhookDelivery:
    account = uuid.uuid4()
    subscribe(db_session, account)
    with get_sessionmaker()() as session:
        handle_event(session, transfer_event(account, uuid.uuid4()))
    delivery = db_session.query(WebhookDelivery).one()
    delivery.status = "DEAD"
    delivery.attempts = 8
    delivery.last_error = "HTTP 500"
    db_session.commit()
    return delivery


def test_admin_routes_require_the_admin_token(database_url: str):
    del database_url
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/dlq").status_code == 401
    with TestClient(app, headers={"X-Admin-Token": "wrong-token-000000"}) as wrong:
        response = wrong.get("/admin/dlq")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_create_subscription_returns_the_secret_once(client: TestClient):
    account_id = str(uuid.uuid4())

    created = client.post(
        "/admin/subscriptions",
        json={"account_id": account_id, "url": "http://localhost:8002/webhooks"},
    )

    assert created.status_code == 201
    assert len(created.json()["secret"]) >= 32
    listed = client.get("/admin/subscriptions", params={"account_id": account_id}).json()
    assert [s["id"] for s in listed] == [created.json()["id"]]
    assert "secret" not in listed[0]


def test_subscription_accepts_a_merchant_supplied_secret(client: TestClient):
    response = client.post(
        "/admin/subscriptions",
        json={
            "account_id": str(uuid.uuid4()),
            "url": "http://localhost:8002/webhooks",
            "secret": "merchant-chosen-secret",
        },
    )

    assert response.status_code == 201
    assert response.json()["secret"] == "merchant-chosen-secret"


def test_dlq_lists_dead_deliveries_and_replay_resets_them(client: TestClient, db_session):
    delivery = dead_delivery(db_session)

    listed = client.get("/admin/dlq").json()
    assert [d["id"] for d in listed] == [str(delivery.id)]

    replayed = client.post(f"/admin/dlq/{delivery.id}/replay")
    assert replayed.status_code == 200
    body = replayed.json()
    assert body["status"] == "PENDING"
    assert body["attempts"] == 0
    assert body["last_error"] is None
    assert client.get("/admin/dlq").json() == []
    assert client.get("/admin/deliveries", params={"status": "pending"}).json()[0]["id"] == str(
        delivery.id
    )


def test_replay_rejects_unknown_and_live_deliveries(client: TestClient, db_session):
    assert client.post(f"/admin/dlq/{uuid.uuid4()}/replay").status_code == 404

    delivery = dead_delivery(db_session)
    client.post(f"/admin/dlq/{delivery.id}/replay")
    second = client.post(f"/admin/dlq/{delivery.id}/replay")
    assert second.status_code == 409
