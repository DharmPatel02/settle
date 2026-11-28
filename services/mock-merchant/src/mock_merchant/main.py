"""A fake merchant endpoint for exercising webhook signatures, retries, and the DLQ.

It verifies every request's signature like a real merchant should, then fails a configurable
share of valid requests on purpose so the sender's retry logic has something to do.
"""

import hashlib
import hmac
import json
import random
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from mock_merchant.telemetry import configure_logging, configure_tracing


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    webhook_secret: str = Field(default="mock-merchant-secret-change-me", min_length=16)
    failure_rate: float = Field(default=0.3, ge=0, le=1)
    # Reject requests signed more than this long ago, so a captured request cannot be replayed.
    timestamp_tolerance_seconds: int = Field(default=300, gt=0)
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class FailureRate(BaseModel):
    failure_rate: Annotated[float, Field(ge=0, le=1)]


class MerchantState:
    def __init__(self, failure_rate: float, rng: random.Random | None = None) -> None:
        self.failure_rate = failure_rate
        self.rng = rng or random.Random()
        self.received: Counter[str] = Counter()
        self.lock = threading.Lock()


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), signature)


def problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
        media_type="application/problem+json",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging("mock-merchant")
    configure_tracing(
        "mock-merchant",
        enabled=settings.otel_enabled,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    yield


app = FastAPI(title="Settle Mock Merchant", version="0.1.0", lifespan=lifespan)
app.state.merchant = MerchantState(get_settings().failure_rate)
Instrumentator(
    excluded_handlers=["/metrics", "/health/.*"],
    should_instrument_requests_inprogress=True,
    inprogress_labels=True,
).instrument(
    app,
    # Millisecond-scale buckets; the library default (0.1, 0.5, 1 s) cannot show a p95 of 20 ms.
    latency_lowr_buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1, 2.5, 5),
).expose(app, include_in_schema=False)
# Continues the trace from the webhook's traceparent header: the last hop of a transfer's trace.
FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics,/health/live")


@app.post("/webhooks")
async def receive_webhook(
    request: Request,
    settle_timestamp: Annotated[str | None, Header()] = None,
    settle_signature: Annotated[str | None, Header()] = None,
):
    settings = get_settings()
    state: MerchantState = request.app.state.merchant
    body = await request.body()

    if settle_timestamp is None or settle_signature is None:
        return problem(401, "Missing signature", "Settle-Timestamp and Settle-Signature required.")
    if not settle_timestamp.isdigit():
        return problem(401, "Invalid timestamp", "Settle-Timestamp must be Unix seconds.")
    if abs(time.time() - int(settle_timestamp)) > settings.timestamp_tolerance_seconds:
        return problem(401, "Stale timestamp", "The request was signed too long ago.")
    if not verify_signature(settings.webhook_secret, settle_timestamp, body, settle_signature):
        return problem(401, "Invalid signature", "The signature does not match the body.")

    with state.lock:
        if state.rng.random() < state.failure_rate:
            return problem(500, "Simulated failure", "Failing on purpose to test retries.")
        event_id = str(json.loads(body).get("event_id"))
        state.received[event_id] += 1
        duplicate = state.received[event_id] > 1
    # A real merchant should dedupe by event_id too, since delivery is at least once.
    return {"received": True, "duplicate": duplicate}


@app.get("/received")
def received(request: Request) -> dict[str, int]:
    state: MerchantState = request.app.state.merchant
    with state.lock:
        return dict(state.received)


@app.put("/config/failure-rate")
def set_failure_rate(update: FailureRate, request: Request) -> FailureRate:
    state: MerchantState = request.app.state.merchant
    with state.lock:
        state.failure_rate = update.failure_rate
    return update


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}
