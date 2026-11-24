import json
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from helpers import subscribe, transfer_event
from sqlalchemy.orm import Session

from webhook_service.db import get_sessionmaker
from webhook_service.delivery_worker import DeliveryWorker, RetryPolicy
from webhook_service.events import handle_event
from webhook_service.kafka import OutgoingMessage, PublishError
from webhook_service.models import WebhookDelivery
from webhook_service.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

DLQ_TOPIC = "transfers.events.dlq"


class Clock:
    def __init__(self) -> None:
        # Start just after real time: deliveries are queued with the real clock, so a fixed
        # date would make them look "not due yet" whenever the test runs before that date.
        self.now = datetime.now(UTC) + timedelta(seconds=1)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingPublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[OutgoingMessage] = []

    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None:
        assert topic == DLQ_TOPIC
        if self.fail:
            raise PublishError("broker down")
        self.messages.extend(messages)


def make_worker(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: Clock,
    publisher: RecordingPublisher | None = None,
    max_attempts: int = 8,
) -> DeliveryWorker:
    return DeliveryWorker(
        get_sessionmaker(),
        httpx.Client(transport=httpx.MockTransport(handler)),
        publisher or RecordingPublisher(),
        dlq_topic=DLQ_TOPIC,
        policy=RetryPolicy(max_attempts=max_attempts),
        clock=clock,
        rng=random.Random(7),
    )


def queue_delivery(db_session: Session) -> uuid.UUID:
    account = uuid.uuid4()
    subscribe(db_session, account)
    with get_sessionmaker()() as session:
        handle_event(session, transfer_event(account, uuid.uuid4()))
    delivery = db_session.query(WebhookDelivery).one()
    return delivery.id


def load(db_session: Session, delivery_id: uuid.UUID) -> WebhookDelivery:
    db_session.expire_all()
    delivery = db_session.get(WebhookDelivery, delivery_id)
    assert delivery is not None
    return delivery


def run_until_due(worker: DeliveryWorker, clock: Clock, db_session: Session, delivery_id) -> None:
    """Jump the clock to the delivery's next attempt and run one pass."""
    delivery = load(db_session, delivery_id)
    clock.now = max(clock.now, delivery.next_attempt_at)
    assert worker.run_once() == 1


def test_request_is_signed_with_the_subscription_secret(db_session: Session):
    delivery_id = queue_delivery(db_session)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    clock = Clock()
    make_worker(handler, clock).run_once()

    request = seen[0]
    timestamp = int(request.headers[TIMESTAMP_HEADER])
    assert timestamp == int(clock.now.timestamp())
    assert request.headers[SIGNATURE_HEADER] == sign(
        "merchant-secret-123456", timestamp, request.content
    )
    assert json.loads(request.content) == load(db_session, delivery_id).payload


def test_delivery_succeeds_after_transient_failures(db_session: Session):
    delivery_id = queue_delivery(db_session)
    responses = iter([503, 500, 200])
    clock = Clock()
    worker = make_worker(lambda request: httpx.Response(next(responses)), clock)

    assert worker.run_once() == 1
    first_failure = load(db_session, delivery_id)
    assert first_failure.status == "PENDING"
    assert first_failure.last_error == "HTTP 503"
    # Not due yet: backoff after attempt 1 is between 0.5 s and 1 s.
    delay = (first_failure.next_attempt_at - clock.now).total_seconds()
    assert 0.5 <= delay <= 1.0
    assert worker.run_once() == 0

    run_until_due(worker, clock, db_session, delivery_id)
    second_failure = load(db_session, delivery_id)
    delay = (second_failure.next_attempt_at - clock.now).total_seconds()
    assert 1.0 <= delay <= 2.0  # doubled

    run_until_due(worker, clock, db_session, delivery_id)
    delivered = load(db_session, delivery_id)
    assert delivered.status == "SUCCEEDED"
    assert delivered.attempts == 3
    assert delivered.last_error is None
    assert delivered.delivered_at == clock.now


def test_network_errors_are_retried(db_session: Session):
    delivery_id = queue_delivery(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    make_worker(handler, Clock()).run_once()

    delivery = load(db_session, delivery_id)
    assert delivery.status == "PENDING"
    assert delivery.last_error.startswith("ConnectError")


def test_delivery_goes_dead_and_to_the_dlq_after_max_attempts(db_session: Session):
    delivery_id = queue_delivery(db_session)
    publisher = RecordingPublisher()
    clock = Clock()
    worker = make_worker(lambda request: httpx.Response(500), clock, publisher)

    for _ in range(8):
        run_until_due(worker, clock, db_session, delivery_id)

    dead = load(db_session, delivery_id)
    assert dead.status == "DEAD"
    assert dead.attempts == 8
    assert dead.dlq_published_at is not None
    assert len(publisher.messages) == 1
    letter = json.loads(publisher.messages[0].value)
    assert letter["delivery_id"] == str(delivery_id)
    assert letter["attempts"] == 8
    assert letter["last_error"] == "HTTP 500"
    # A dead delivery is never attempted again by the worker.
    clock.advance(10_000)
    assert worker.run_once() == 0


def test_dlq_publish_is_retried_if_kafka_is_down(db_session: Session):
    delivery_id = queue_delivery(db_session)
    clock = Clock()
    failing = RecordingPublisher(fail=True)
    worker = make_worker(lambda request: httpx.Response(500), clock, failing, max_attempts=1)

    with pytest.raises(PublishError):
        worker.run_once()
    assert load(db_session, delivery_id).status == "DEAD"
    assert load(db_session, delivery_id).dlq_published_at is None

    healthy = RecordingPublisher()
    assert make_worker(lambda r: httpx.Response(500), clock, healthy).publish_dead_letters() == 1
    assert load(db_session, delivery_id).dlq_published_at is not None


def test_expired_lease_makes_a_delivery_due_again(db_session: Session):
    delivery_id = queue_delivery(db_session)
    clock = Clock()
    worker = make_worker(lambda request: httpx.Response(200), clock)

    # Simulate a worker that claimed the row and then crashed before sending.
    assert len(worker.claim_due()) == 1
    assert worker.run_once() == 0  # still leased
    clock.advance(31)

    assert worker.run_once() == 1
    delivered = load(db_session, delivery_id)
    assert delivered.status == "SUCCEEDED"
    assert delivered.attempts == 2
