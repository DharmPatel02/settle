from fastapi.testclient import TestClient

from payments_api.main import app
from payments_api.rate_limit import (
    RateLimitDecision,
    TokenBucketRateLimiter,
    get_transfer_limiter,
)


class DenyRateLimiter:
    def consume(self, client_id: str) -> RateLimitDecision:
        del client_id
        return RateLimitDecision(allowed=False, retry_after_seconds=3)


class BrokenRateLimiter:
    def consume(self, client_id: str) -> RateLimitDecision:
        del client_id
        raise ConnectionError("Redis is down")


class StubRedis:
    def __init__(self, result: list[int]) -> None:
        self.result = result

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[int]:
        assert "HMGET" in script
        assert numkeys == 1
        assert keys_and_args[0] == "settle:transfer-rate:client-1"
        return self.result


def test_retry_after_rounds_up_to_whole_seconds():
    limiter = TokenBucketRateLimiter(StubRedis([0, 1_001]), capacity=2, refill_per_second=1)

    decision = limiter.consume("client-1", now_ms=1_000)

    assert not decision.allowed
    assert decision.retry_after_seconds == 2


def create_transfer_request(client: TestClient) -> dict[str, object]:
    source = client.post("/v1/accounts", json={"owner_name": "Source", "currency": "USD"}).json()
    destination = client.post(
        "/v1/accounts", json={"owner_name": "Destination", "currency": "USD"}
    ).json()
    return {
        "from_account_id": source["id"],
        "to_account_id": destination["id"],
        "amount_minor": 1,
        "currency": "USD",
    }


def test_rate_limit_returns_429_and_retry_after(client: TestClient):
    body = create_transfer_request(client)
    app.dependency_overrides[get_transfer_limiter] = lambda: DenyRateLimiter()

    response = client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "rate-limited"},
        json=body,
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_redis_failure_fails_closed_with_503(client: TestClient):
    body = create_transfer_request(client)
    app.dependency_overrides[get_transfer_limiter] = lambda: BrokenRateLimiter()

    response = client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": "redis-down"},
        json=body,
    )

    assert response.status_code == 503
    assert response.json()["title"] == "Rate limiter unavailable"
