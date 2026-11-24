from fastapi.testclient import TestClient

from webhook_service.main import app


def test_live_needs_nothing_external():
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}


def test_ready_checks_the_database(client: TestClient):
    assert client.get("/health/ready").json() == {"status": "ok"}
