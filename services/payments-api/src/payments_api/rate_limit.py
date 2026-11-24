import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Protocol

from fastapi import Depends

from payments_api.config import Settings, get_settings
from payments_api.errors import Problem
from payments_api.security import ApiClientPrincipal, get_api_client

TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local values = redis.call('HMGET', key, 'tokens', 'updated_at_ms')
local tokens = tonumber(values[1]) or capacity
local updated_at_ms = tonumber(values[2]) or now_ms
local elapsed_ms = math.max(0, now_ms - updated_at_ms)
tokens = math.min(capacity, tokens + (elapsed_ms * refill_per_ms))

local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
    allowed = 1
    tokens = tokens - 1
else
    retry_after_ms = math.ceil((1 - tokens) / refill_per_ms)
end

redis.call('HSET', key, 'tokens', tokens, 'updated_at_ms', now_ms)
redis.call('PEXPIRE', key, ttl_ms)
return {allowed, retry_after_ms}
"""


class RedisEvalClient(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> list[int]: ...


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class TokenBucketRateLimiter:
    def __init__(
        self,
        redis_client: RedisEvalClient,
        *,
        capacity: int,
        refill_per_second: float,
    ) -> None:
        self.redis_client = redis_client
        self.capacity = capacity
        self.refill_per_second = refill_per_second

    def consume(self, client_id: str, *, now_ms: int | None = None) -> RateLimitDecision:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        refill_per_ms = self.refill_per_second / 1000
        ttl_ms = max(60_000, int((self.capacity / self.refill_per_second) * 2000))
        result = self.redis_client.eval(
            TOKEN_BUCKET_SCRIPT,
            1,
            f"settle:transfer-rate:{client_id}",
            self.capacity,
            refill_per_ms,
            current_ms,
            ttl_ms,
        )
        allowed, retry_after_ms = int(result[0]), int(result[1])
        retry_after_seconds = max(1, (retry_after_ms + 999) // 1000) if not allowed else 0
        return RateLimitDecision(bool(allowed), retry_after_seconds)


@lru_cache
def get_redis_client():
    # Imported lazily so liveness checks and management commands that do not
    # use Redis can still start independently.
    from redis import Redis

    return Redis.from_url(str(get_settings().redis_url), decode_responses=True)


def get_transfer_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenBucketRateLimiter:
    return TokenBucketRateLimiter(
        get_redis_client(),
        capacity=settings.transfer_rate_capacity,
        refill_per_second=settings.transfer_rate_refill_per_second,
    )


def require_transfer_capacity(
    client: Annotated[ApiClientPrincipal, Depends(get_api_client)],
    limiter: Annotated[TokenBucketRateLimiter, Depends(get_transfer_limiter)],
) -> ApiClientPrincipal:
    try:
        decision = limiter.consume(str(client.id))
    except Exception as exc:
        raise Problem(
            503,
            "Rate limiter unavailable",
            "Transfers are temporarily unavailable because rate limiting cannot be verified.",
        ) from exc
    if not decision.allowed:
        raise Problem(
            429,
            "Rate limit exceeded",
            "Too many transfer requests. Retry later.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    return client
