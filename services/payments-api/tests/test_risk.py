import json
from collections.abc import Iterator

import httpx
import pybreaker
import pytest
import respx
from conftest import TEST_ADMIN_API_KEY
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payments_api.main import app
from payments_api.models import LedgerEntry, OutboxEvent
from payments_api.risk import FallbackPolicy, RiskClient, get_risk_scorer

RISK_URL = "http://risk.test"
ADMIN = {"X-API-Key": TEST_ADMIN_API_KEY}
FALLBACK_LIMIT = 10_000


def score_reply(decision: str, score: float) -> httpx.Response:
    return httpx.Response(
        200, json={"score": score, "decision": decision, "model_version": "risk-hgb-v1"}
    )


@pytest.fixture()
def risk_api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=RISK_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture()
def breaker() -> pybreaker.CircuitBreaker:
    return pybreaker.CircuitBreaker(fail_max=2, reset_timeout=60)


@pytest.fixture(autouse=True)
def real_risk_client(breaker: pybreaker.CircuitBreaker) -> Iterator[None]:
    # The real client, with risk-service replaced by respx at the HTTP layer.
    risk = RiskClient(
        httpx.Client(base_url=RISK_URL, timeout=0.15),
        breaker,
        FallbackPolicy(approve_up_to_minor=FALLBACK_LIMIT),
    )
    app.dependency_overrides[get_risk_scorer] = lambda: risk
    yield
    app.dependency_overrides.pop(get_risk_scorer, None)


def create_account(client: TestClient, owner: str) -> str:
    response = client.post("/v1/accounts", json={"owner_name": owner, "currency": "USD"})
    assert response.status_code == 201
    return response.json()["id"]


def funded_pair(client: TestClient, amount: int = 100_000) -> tuple[str, str]:
    alice, bob = create_account(client, "Alice"), create_account(client, "Bob")
    response = client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": f"fund-{alice}"},
        json={"account_id": alice, "amount_minor": amount, "currency": "USD"},
    )
    assert response.status_code == 201
    return alice, bob


def send(client: TestClient, source: str, destination: str, amount: int, key: str):
    return client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": key},
        json={
            "from_account_id": source,
            "to_account_id": destination,
            "amount_minor": amount,
            "currency": "USD",
        },
    )


def balance(client: TestClient, account_id: str) -> int:
    return client.get(f"/v1/accounts/{account_id}").json()["balance_minor"]


def event_types(db_session: Session, transfer_id: str) -> list[str]:
    db_session.expire_all()
    return list(
        db_session.scalars(
            select(OutboxEvent.event_type)
            .where(OutboxEvent.aggregate_id == transfer_id)
            .order_by(OutboxEvent.created_at)
        )
    )


def ledger_entries(db_session: Session, transfer_id: str) -> int:
    return db_session.scalar(
        select(func.count()).select_from(LedgerEntry).where(LedgerEntry.transfer_id == transfer_id)
    )


def test_approved_transfer_stores_the_score_and_sends_features(
    client: TestClient, risk_api: respx.MockRouter
):
    route = risk_api.post("/v1/score").mock(return_value=score_reply("APPROVE", 0.0012))
    alice, bob = funded_pair(client)

    first = send(client, alice, bob, 2_500, "t-1")
    second = send(client, alice, bob, 3_500, "t-2")

    assert first.status_code == second.status_code == 201
    assert first.json()["risk_score"] == pytest.approx(0.0012)
    assert first.json()["risk_model_version"] == "risk-hgb-v1"
    sent_first = json.loads(route.calls[0].request.content)
    sent_second = json.loads(route.calls[1].request.content)
    assert sent_first["amount_minor"] == 2_500
    assert sent_first["is_new_recipient"] is True
    assert sent_first["transfers_last_hour"] == 0
    assert sent_first["avg_amount_30d_minor"] == 0
    assert 0 <= sent_first["account_age_days"] < 1
    # The first transfer is now history: Bob is a known recipient with one recent transfer.
    assert sent_second["is_new_recipient"] is False
    assert sent_second["transfers_last_hour"] == 1
    assert sent_second["avg_amount_30d_minor"] == 2_500
    assert balance(client, bob) == 6_000


def test_declined_transfer_moves_no_money(
    client: TestClient, db_session: Session, risk_api: respx.MockRouter
):
    risk_api.post("/v1/score").mock(return_value=score_reply("DECLINE", 0.97))
    alice, bob = funded_pair(client)

    response = send(client, alice, bob, 50_000, "t-decline")

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["title"] == "Transfer declined"
    transfer_id = response.json()["transfer_id"]
    transfer = client.get(f"/v1/transfers/{transfer_id}").json()
    assert transfer["status"] == "DECLINED"
    assert transfer["failure_reason"] == "RISK_DECLINED"
    assert transfer["risk_score"] == pytest.approx(0.97)
    assert ledger_entries(db_session, transfer_id) == 0
    assert event_types(db_session, transfer_id) == ["TransferDeclined"]
    assert balance(client, alice) == 100_000


def test_review_holds_the_transfer_until_an_admin_approves(
    client: TestClient, db_session: Session, risk_api: respx.MockRouter
):
    risk_api.post("/v1/score").mock(return_value=score_reply("REVIEW", 0.2))
    alice, bob = funded_pair(client)

    held = send(client, alice, bob, 30_000, "t-review")

    assert held.status_code == 202
    transfer_id = held.json()["id"]
    assert held.json()["status"] == "PENDING_REVIEW"
    assert balance(client, alice) == 100_000
    assert ledger_entries(db_session, transfer_id) == 0
    queue = client.get("/admin/transfers/review", headers=ADMIN).json()
    assert [item["id"] for item in queue] == [transfer_id]

    approved = client.post(f"/admin/transfers/{transfer_id}/approve", headers=ADMIN)

    assert approved.status_code == 200
    assert approved.json()["status"] == "COMPLETED"
    assert approved.json()["risk_score"] == pytest.approx(0.2)  # the original score is kept
    assert balance(client, alice) == 70_000
    assert balance(client, bob) == 30_000
    assert ledger_entries(db_session, transfer_id) == 2
    assert event_types(db_session, transfer_id) == ["TransferPendingReview", "TransferCompleted"]
    assert client.get("/admin/transfers/review", headers=ADMIN).json() == []

    # Approving twice must not move the money twice.
    again = client.post(f"/admin/transfers/{transfer_id}/approve", headers=ADMIN)
    assert again.status_code == 409
    assert balance(client, alice) == 70_000


def test_admin_can_decline_a_held_transfer(
    client: TestClient, db_session: Session, risk_api: respx.MockRouter
):
    risk_api.post("/v1/score").mock(return_value=score_reply("REVIEW", 0.2))
    alice, bob = funded_pair(client)
    transfer_id = send(client, alice, bob, 30_000, "t-review").json()["id"]

    declined = client.post(f"/admin/transfers/{transfer_id}/decline", headers=ADMIN)

    assert declined.status_code == 200
    assert declined.json()["status"] == "DECLINED"
    assert declined.json()["failure_reason"] == "REVIEW_DECLINED"
    assert event_types(db_session, transfer_id) == ["TransferPendingReview", "TransferDeclined"]
    assert client.post(f"/admin/transfers/{transfer_id}/approve", headers=ADMIN).status_code == 409
    assert balance(client, alice) == 100_000


def test_approval_rechecks_the_balance(
    client: TestClient, db_session: Session, risk_api: respx.MockRouter
):
    route = risk_api.post("/v1/score").mock(return_value=score_reply("REVIEW", 0.2))
    alice, bob = funded_pair(client, amount=40_000)
    held_id = send(client, alice, bob, 30_000, "t-review").json()["id"]
    # While the first transfer waits, the money is spent elsewhere.
    route.mock(return_value=score_reply("APPROVE", 0.001))
    assert send(client, alice, bob, 20_000, "t-spend").status_code == 201

    response = client.post(f"/admin/transfers/{held_id}/approve", headers=ADMIN)

    assert response.status_code == 422
    assert response.json()["title"] == "Insufficient funds"
    transfer = client.get(f"/v1/transfers/{held_id}").json()
    assert transfer["status"] == "REJECTED"
    assert balance(client, alice) == 20_000
    assert event_types(db_session, held_id) == ["TransferPendingReview", "TransferRejected"]


def test_review_endpoints_need_an_admin_key(client: TestClient):
    assert client.get("/admin/transfers/review").status_code == 403
    unknown = "00000000-0000-0000-0000-00000000dead"
    assert client.post(f"/admin/transfers/{unknown}/approve").status_code == 403
    assert client.post(f"/admin/transfers/{unknown}/approve", headers=ADMIN).status_code == 404


@pytest.mark.parametrize(
    ("amount", "status_code", "status"),
    [(FALLBACK_LIMIT, 201, "COMPLETED"), (FALLBACK_LIMIT + 1, 202, "PENDING_REVIEW")],
)
def test_timeout_uses_the_fallback_policy(
    client: TestClient, risk_api: respx.MockRouter, amount: int, status_code: int, status: str
):
    risk_api.post("/v1/score").mock(side_effect=httpx.ReadTimeout("too slow"))
    alice, bob = funded_pair(client)

    response = send(client, alice, bob, amount, "t-timeout")

    assert response.status_code == status_code
    assert response.json()["status"] == status
    assert response.json()["risk_model_version"] == "fallback"
    assert response.json()["risk_score"] is None


def test_open_circuit_skips_the_call(
    client: TestClient, risk_api: respx.MockRouter, breaker: pybreaker.CircuitBreaker
):
    route = risk_api.post("/v1/score").mock(return_value=httpx.Response(500))
    alice, bob = funded_pair(client)

    for index in range(2):
        assert send(client, alice, bob, 100, f"t-fail-{index}").status_code == 201
    assert breaker.current_state == "open"
    assert route.call_count == 2

    response = send(client, alice, bob, 100, "t-open")

    # Answered by the fallback straight away, without waiting on a broken service.
    assert response.status_code == 201
    assert response.json()["risk_model_version"] == "fallback"
    assert route.call_count == 2


def test_invalid_risk_response_uses_the_fallback(client: TestClient, risk_api: respx.MockRouter):
    risk_api.post("/v1/score").mock(return_value=httpx.Response(200, json={"decision": "MAYBE"}))
    alice, bob = funded_pair(client)

    response = send(client, alice, bob, 50_000, "t-bad")

    assert response.status_code == 202
    assert response.json()["risk_model_version"] == "fallback"


def test_replays_deposits_and_unknown_accounts_do_not_call_risk(
    client: TestClient, risk_api: respx.MockRouter
):
    route = risk_api.post("/v1/score").mock(return_value=score_reply("APPROVE", 0.001))
    alice, bob = funded_pair(client)  # a deposit: not scored
    assert route.call_count == 0

    first = send(client, alice, bob, 100, "t-1")
    replay = send(client, alice, bob, 100, "t-1")
    assert replay.json() == first.json()
    assert route.call_count == 1

    unknown = "00000000-0000-0000-0000-00000000beef"
    assert send(client, alice, unknown, 100, "t-unknown").status_code == 404
    assert route.call_count == 1
