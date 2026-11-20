from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from payments_api.db import get_session
from payments_api.main import app

client = TestClient(app)


def test_live_returns_ok():
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


class BrokenSession:
    """Stands in for a session whose database is down."""

    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_ready_returns_503_when_database_is_down():
    app.dependency_overrides[get_session] = lambda: BrokenSession()
    try:
        response = client.get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
