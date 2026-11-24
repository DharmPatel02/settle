import uuid
from collections.abc import Iterator

import pytest

pytest.importorskip("redis")

from testcontainers.community.redis import RedisContainer

from payments_api.rate_limit import TokenBucketRateLimiter


@pytest.fixture(scope="module")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("redis:7") as container:
        yield container


def test_burst_is_limited(redis_container: RedisContainer):
    limiter = TokenBucketRateLimiter(redis_container.get_client(), capacity=2, refill_per_second=1)
    client_id = str(uuid.uuid4())

    assert limiter.consume(client_id, now_ms=1_000).allowed
    assert limiter.consume(client_id, now_ms=1_000).allowed
    denied = limiter.consume(client_id, now_ms=1_000)

    assert not denied.allowed
    assert denied.retry_after_seconds == 1


def test_token_refills_over_time(redis_container: RedisContainer):
    limiter = TokenBucketRateLimiter(redis_container.get_client(), capacity=1, refill_per_second=1)
    client_id = str(uuid.uuid4())

    assert limiter.consume(client_id, now_ms=10_000).allowed
    assert not limiter.consume(client_id, now_ms=10_000).allowed
    assert limiter.consume(client_id, now_ms=11_000).allowed
