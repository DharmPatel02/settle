import hashlib
import hmac
import json
import random
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mock_merchant.main import MerchantState, app, get_settings

SECRET = get_settings().webhook_secret


def signed(body: bytes, timestamp: int | None = None, secret: str = SECRET) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return {
        "Content-Type": "application/json",
        "Settle-Timestamp": str(timestamp),
        "Settle-Signature": signature.hexdigest(),
    }


@pytest.fixture()
def client() -> Iterator[TestClient]:
    app.state.merchant = MerchantState(failure_rate=0.0, rng=random.Random(3))
    with TestClient(app) as test_client:
        yield test_client


BODY = json.dumps({"event_id": "evt-1", "event_type": "TransferCompleted"}).encode()


def test_valid_signature_is_accepted_and_duplicates_are_flagged(client: TestClient):
    first = client.post("/webhooks", content=BODY, headers=signed(BODY))
    second = client.post("/webhooks", content=BODY, headers=signed(BODY))

    assert first.json() == {"received": True, "duplicate": False}
    assert second.json() == {"received": True, "duplicate": True}
    assert client.get("/received").json() == {"evt-1": 2}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        signed(BODY, secret="a-different-secret-value"),
        signed(b'{"event_id":"tampered"}'),
        signed(BODY, timestamp=int(time.time()) - 3600),
        {"Settle-Timestamp": "yesterday", "Settle-Signature": "abc"},
    ],
    ids=["missing", "wrong-secret", "tampered-body", "stale", "bad-timestamp"],
)
def test_bad_or_missing_signatures_are_rejected(client: TestClient, headers: dict[str, str]):
    response = client.post("/webhooks", content=BODY, headers=headers)

    assert response.status_code == 401
    assert client.get("/received").json() == {}


def test_failure_rate_makes_valid_requests_fail(client: TestClient):
    assert client.put("/config/failure-rate", json={"failure_rate": 1.0}).status_code == 200

    response = client.post("/webhooks", content=BODY, headers=signed(BODY))

    assert response.status_code == 500
    assert client.get("/received").json() == {}


def test_failure_rate_must_be_a_probability(client: TestClient):
    assert client.put("/config/failure-rate", json={"failure_rate": 1.5}).status_code == 422


def test_live(client: TestClient):
    assert client.get("/health/live").json() == {"status": "ok"}


def test_metrics_endpoint(client: TestClient):
    client.get("/received")
    assert 'http_requests_total{handler="/received"' in client.get("/metrics").text
