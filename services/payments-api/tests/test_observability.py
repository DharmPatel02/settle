import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pybreaker
import pytest
import respx
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from payments_api.db import get_engine, get_sessionmaker
from payments_api.kafka import OutgoingMessage
from payments_api.models import OutboxEvent
from payments_api.outbox_relay import OutboxRelay
from payments_api.reconciliation import run_once
from payments_api.risk import (
    BreakerStateMetric,
    FallbackPolicy,
    RiskClient,
    RiskFeatures,
)

TOPIC = "transfers.events"


@pytest.fixture(scope="module")
def spans() -> Iterator[InMemorySpanExporter]:
    # The global tracer provider can be set once per process; every later test then records
    # spans in memory, which is harmless.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[OutgoingMessage] = []

    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None:
        assert topic == TOPIC
        self.messages.extend(messages)


def sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def create_account(client: TestClient, owner: str) -> str:
    return client.post("/v1/accounts", json={"owner_name": owner, "currency": "USD"}).json()["id"]


def make_transfer(client: TestClient, key: str) -> str:
    alice, bob = create_account(client, "Alice"), create_account(client, "Bob")
    client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": f"fund-{key}"},
        json={"account_id": alice, "amount_minor": 1_000, "currency": "USD"},
    )
    response = client.post(
        "/v1/transfers",
        headers={"Idempotency-Key": key},
        json={
            "from_account_id": alice,
            "to_account_id": bob,
            "amount_minor": 100,
            "currency": "USD",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_metrics_endpoint_exposes_http_and_business_metrics(client: TestClient):
    completed_before = sample("settle_transfers_total", {"status": "COMPLETED"})

    make_transfer(client, "t-metrics")
    body = client.get("/metrics").text

    # The deposit and the transfer both complete.
    assert sample("settle_transfers_total", {"status": "COMPLETED"}) == completed_before + 2
    assert "settle_transfer_duration_seconds_bucket" in body
    assert 'http_requests_total{handler="/v1/transfers",method="POST",status="2xx"}' in body
    assert "settle_db_pool_checked_out" in body
    assert "settle_db_pool_size" in body


def test_replays_are_not_counted_twice(client: TestClient):
    alice, bob = create_account(client, "Alice"), create_account(client, "Bob")
    client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": "fund-replay"},
        json={"account_id": alice, "amount_minor": 1_000, "currency": "USD"},
    )
    body = {"from_account_id": alice, "to_account_id": bob, "amount_minor": 100, "currency": "USD"}
    client.post("/v1/transfers", headers={"Idempotency-Key": "t-replay"}, json=body)
    completed = sample("settle_transfers_total", {"status": "COMPLETED"})

    replay = client.post("/v1/transfers", headers={"Idempotency-Key": "t-replay"}, json=body)

    assert replay.status_code == 201
    assert sample("settle_transfers_total", {"status": "COMPLETED"}) == completed


def test_risk_fallbacks_and_breaker_state_are_exported():
    breaker = pybreaker.CircuitBreaker(
        fail_max=1, reset_timeout=60, listeners=[BreakerStateMetric()]
    )
    risk = RiskClient(
        httpx.Client(base_url="http://risk.test"), breaker, FallbackPolicy(approve_up_to_minor=1)
    )
    features = RiskFeatures(100, 12, 30.0, 0, 0, True)
    before = sample("settle_risk_fallbacks_total", {"reason": "circuit_open"})

    with respx.mock(base_url="http://risk.test") as mock:
        mock.post("/v1/score").mock(return_value=httpx.Response(500))
        risk.assess(features)  # this failure trips the breaker

    assert sample("settle_risk_circuit_state") == 2
    assert sample("settle_risk_fallbacks_total", {"reason": "circuit_open"}) == before + 1
    assert sample("settle_risk_latency_seconds_count", {"source": "fallback"}) >= 1

    breaker.close()
    assert sample("settle_risk_circuit_state") == 0


def test_outbox_lag_is_the_age_of_the_oldest_unpublished_event(db_session: Session):
    relay = OutboxRelay(get_sessionmaker(), RecordingPublisher(), topic=TOPIC)
    assert relay.measure_lag() == 0
    db_session.add(
        OutboxEvent(
            aggregate_type="transfer",
            aggregate_id=uuid.uuid4(),
            event_type="TransferCompleted",
            partition_key="a",
            payload={},
            created_at=datetime.now(UTC) - timedelta(seconds=45),
        )
    )
    db_session.commit()

    assert 45 <= relay.measure_lag() < 60
    assert 45 <= sample("settle_outbox_lag_seconds") < 60

    relay.publish_batch()
    assert relay.measure_lag() == 0


def test_reconciliation_finds_a_corrupted_balance(client: TestClient):
    make_transfer(client, "t-reconcile")
    assert run_once(get_sessionmaker()).mismatches == 0
    assert sample("settle_reconciliation_mismatches") == 0

    engine = get_engine()
    with engine.begin() as connection:
        # Simulate a bug that changes a balance without a ledger entry.
        account_id = connection.scalar(
            text("SELECT id FROM accounts WHERE owner_name = 'Bob' LIMIT 1")
        )
        connection.execute(
            text("UPDATE accounts SET balance_minor = balance_minor + 1 WHERE id = :id"),
            {"id": account_id},
        )
    try:
        result = run_once(get_sessionmaker())
        assert result.balance_mismatches == 1
        assert result.ledger_sum == 0
        assert sample("settle_reconciliation_mismatches") == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE accounts SET balance_minor = balance_minor - 1 WHERE id = :id"),
                {"id": account_id},
            )
    assert run_once(get_sessionmaker()).mismatches == 0


def test_trace_continues_from_request_through_outbox_to_kafka_headers(
    client: TestClient, db_session: Session, spans: InMemorySpanExporter
):
    spans.clear()
    transfer_id = make_transfer(client, "t-trace")
    event = db_session.scalars(
        select(OutboxEvent).where(OutboxEvent.aggregate_id == transfer_id)
    ).one()
    assert event.trace_context is not None
    request_trace_id = event.trace_context["traceparent"].split("-")[1]
    server_spans = [
        span
        for span in spans.get_finished_spans()
        if span.kind == SpanKind.SERVER
        and format(span.context.trace_id, "032x") == request_trace_id
    ]
    assert server_spans, "the outbox row should carry the HTTP request's trace"
    sql = [
        span
        for span in spans.get_finished_spans()
        if span.attributes.get("db.system") == "postgresql"
        and format(span.context.trace_id, "032x") == request_trace_id
    ]
    assert {"SELECT", "INSERT", "UPDATE"} <= {span.name for span in sql}

    publisher = RecordingPublisher()
    OutboxRelay(get_sessionmaker(), publisher, topic=TOPIC).publish_batch()

    message = next(m for m in publisher.messages if m.headers["event_id"] == str(event.id))
    assert message.headers["traceparent"].split("-")[1] == request_trace_id
    producer = next(
        span
        for span in spans.get_finished_spans()
        if span.kind == SpanKind.PRODUCER and span.attributes["settle.event_id"] == str(event.id)
    )
    assert format(producer.context.trace_id, "032x") == request_trace_id
