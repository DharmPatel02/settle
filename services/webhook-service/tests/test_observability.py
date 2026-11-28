import json
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import subscribe, transfer_event
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY
from sqlalchemy.orm import Session

from webhook_service.consumer import EventConsumer
from webhook_service.db import get_sessionmaker
from webhook_service.delivery_worker import DeliveryWorker, RetryPolicy
from webhook_service.events import handle_event
from webhook_service.kafka import OutgoingMessage
from webhook_service.models import WebhookDelivery

# A traceparent as the outbox relay would send it: version-trace id-span id-flags.
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture(scope="module")
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


def sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


class FakeMessage:
    def __init__(self, value: bytes, headers: list[tuple[str, bytes]]) -> None:
        self._value = value
        self._headers = headers

    def error(self) -> None:
        return None

    def value(self) -> bytes:
        return self._value

    def headers(self) -> list[tuple[str, bytes]]:
        return self._headers

    def topic(self) -> str:
        return "transfers.events"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return 7


class FakeConsumer:
    def __init__(self, message: FakeMessage) -> None:
        self.message: FakeMessage | None = message
        self.committed: list[FakeMessage] = []

    def poll(self, _timeout: float) -> FakeMessage | None:
        message, self.message = self.message, None
        return message

    def commit(self, message: FakeMessage, asynchronous: bool) -> None:
        self.committed.append(message)


class NullPublisher:
    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None:
        pass


def consume(event: dict[str, object], headers: list[tuple[str, bytes]]) -> FakeConsumer:
    consumer = FakeConsumer(FakeMessage(json.dumps(event).encode(), headers))
    assert EventConsumer(consumer, get_sessionmaker()).process_one()  # type: ignore[arg-type]
    return consumer


def test_one_trace_runs_from_kafka_headers_to_the_merchant_request(
    db_session: Session, spans: InMemorySpanExporter
):
    spans.clear()
    account = uuid.uuid4()
    subscribe(db_session, account)

    consumer = consume(
        transfer_event(account, uuid.uuid4()), [("traceparent", TRACEPARENT.encode())]
    )

    assert len(consumer.committed) == 1
    delivery = db_session.query(WebhookDelivery).one()
    assert delivery.trace_context["traceparent"].split("-")[1] == TRACE_ID
    consumer_span = next(s for s in spans.get_finished_spans() if s.kind == SpanKind.CONSUMER)
    assert format(consumer_span.context.trace_id, "032x") == TRACE_ID

    seen: list[httpx.Request] = []

    def merchant(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(merchant))
    HTTPXClientInstrumentor.instrument_client(http)
    DeliveryWorker(get_sessionmaker(), http, NullPublisher(), dlq_topic="dlq").run_once()

    # The merchant receives the same trace id the payments API started with.
    assert seen[0].headers["traceparent"].split("-")[1] == TRACE_ID
    names = {
        s.name for s in spans.get_finished_spans() if format(s.context.trace_id, "032x") == TRACE_ID
    }
    assert {"process transfers.events", "deliver webhook"} <= names


def test_consumer_counts_processed_duplicate_and_invalid_events(db_session: Session):
    before = {
        r: sample("settle_events_consumed_total", {"result": r})
        for r in ("processed", "duplicate", "invalid")
    }
    account = uuid.uuid4()
    subscribe(db_session, account)
    event = transfer_event(account, uuid.uuid4())

    consume(event, [])
    consume(event, [])  # redelivery of the same event
    consume(transfer_event(uuid.uuid4(), uuid.uuid4()), [])  # nobody subscribed: still processed
    consume({"not": "an event"}, [])

    assert (
        sample("settle_events_consumed_total", {"result": "processed"}) == before["processed"] + 2
    )
    assert (
        sample("settle_events_consumed_total", {"result": "duplicate"}) == before["duplicate"] + 1
    )
    assert sample("settle_events_consumed_total", {"result": "invalid"}) == before["invalid"] + 1


def test_delivery_results_and_dlq_size_are_exported(db_session: Session):
    account = uuid.uuid4()
    subscribe(db_session, account)
    with get_sessionmaker()() as session:
        handle_event(session, transfer_event(account, uuid.uuid4()))
    retries = sample("settle_webhook_deliveries_total", {"result": "retry"})
    dead = sample("settle_webhook_deliveries_total", {"result": "dead"})
    now = datetime.now(UTC) + timedelta(seconds=1)
    worker = DeliveryWorker(
        get_sessionmaker(),
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        NullPublisher(),
        dlq_topic="dlq",
        policy=RetryPolicy(max_attempts=2, base_seconds=0.001),
        clock=lambda: now,
        rng=random.Random(1),
    )

    worker.run_once()
    assert sample("settle_webhook_backlog") == 0  # retry is scheduled in the future
    now += timedelta(seconds=10)
    worker.run_once()

    assert sample("settle_webhook_deliveries_total", {"result": "retry"}) == retries + 1
    assert sample("settle_webhook_deliveries_total", {"result": "dead"}) == dead + 1
    assert sample("settle_dlq_size") == 1
    assert sample("settle_webhook_delivery_duration_seconds_count") >= 2


def test_admin_api_exposes_metrics(client: TestClient):
    client.get("/admin/deliveries")
    body = client.get("/metrics").text
    assert 'http_requests_total{handler="/admin/deliveries"' in body
