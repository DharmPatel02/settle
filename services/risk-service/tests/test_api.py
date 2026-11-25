from fastapi.testclient import TestClient
from helpers import ACCOUNT_TAKEOVER, NORMAL

from risk_service.main import app


def test_normal_transfer_is_approved(client: TestClient):
    response = client.post("/v1/score", json=NORMAL)

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "APPROVE"
    assert 0 <= body["score"] < 0.01
    assert body["model_version"] == "risk-hgb-v1"


def test_account_takeover_pattern_is_declined(client: TestClient):
    body = client.post("/v1/score", json=ACCOUNT_TAKEOVER).json()

    assert body["decision"] == "DECLINE"
    assert body["score"] > 0.9


def test_invalid_features_are_a_400_problem(client: TestClient):
    for bad in ({**NORMAL, "hour_of_day": 24}, {**NORMAL, "amount_minor": 0}, {"amount_minor": 1}):
        response = client.post("/v1/score", json=bad)
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/problem+json"


def test_unknown_fields_are_rejected(client: TestClient):
    # A typo in a feature name must fail loudly, not be silently scored without it.
    response = client.post("/v1/score", json={**NORMAL, "amount": 4000})
    assert response.status_code == 400


def test_health(client: TestClient):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ok"}


def test_without_a_loaded_model_the_service_is_not_ready():
    # No `with`: the lifespan never runs, so no model is loaded.
    client = TestClient(app)
    assert client.get("/health/ready").status_code == 503
    response = client.post("/v1/score", json=NORMAL)
    assert response.status_code == 503
    assert response.json()["title"] == "Model not loaded"
