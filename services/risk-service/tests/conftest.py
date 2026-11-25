import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from helpers import SERVICE_ROOT

from risk_service.config import get_settings
from risk_service.main import app

os.environ["MODEL_PATH"] = str(SERVICE_ROOT / "models" / "risk_model.joblib")
get_settings.cache_clear()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
