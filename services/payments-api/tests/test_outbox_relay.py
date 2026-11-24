import json
import uuid
from collections.abc import Iterator

import pytest
from confluent_kafka import Consumer
from sqlalchemy import select
from sqlalchemy.orm import Session
from testcontainers.community.kafka import KafkaContainer

from payments_api.db import get_sessionmaker
from payments_api.kafka import KafkaPublisher, OutgoingMessage, PublishError, ensure_topic
from payments_api.models import OutboxEvent
from payments_api.outbox_relay import OutboxRelay

TOPIC = "transfers.events"


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[OutgoingMessage] = []

    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None:
        assert topic == TOPIC
        self.messages.extend(messages)


class FailingPublisher:
    def publish(self, topic: str, messages: list[OutgoingMessage]) -> None:
        raise PublishError("broker unavailable")


def add_events(db_session: Session, count: int) -> list[uuid.UUID]:
    events = [
        OutboxEvent(
            aggregate_type="transfer",
            aggregate_id=uuid.uuid4(),
            event_type="TransferCompleted",
            partition_key=f"account-{index}",
            payload={"index": index},
        )
        for index in range(count)
    ]
    db_session.add_all(events)
    db_session.commit()
    return [event.id for event in events]


def unpublished_count(db_session: Session) -> int:
    db_session.expire_all()
    return len(
        db_session.scalars(select(OutboxEvent).where(OutboxEvent.published_at.is_(None))).all()
    )


def test_relay_publishes_events_and_marks_them(db_session: Session):
    event_ids = add_events(db_session, 3)
    publisher = RecordingPublisher()
    relay = OutboxRelay(get_sessionmaker(), publisher, topic=TOPIC)

    assert relay.publish_batch() == 3
    assert relay.publish_batch() == 0

    assert [m.headers["event_id"] for m in publisher.messages] == [str(i) for i in event_ids]
    envelope = json.loads(publisher.messages[0].value)
    assert envelope["event_type"] == "TransferCompleted"
    assert envelope["data"] == {"index": 0}
    assert publisher.messages[0].key == "account-0"
    assert unpublished_count(db_session) == 0


def test_failed_publish_keeps_events_for_the_next_relay(db_session: Session):
    add_events(db_session, 2)

    with pytest.raises(PublishError):
        OutboxRelay(get_sessionmaker(), FailingPublisher(), topic=TOPIC).publish_batch()
    assert unpublished_count(db_session) == 2

    # A restarted relay picks up exactly where the failed one left off.
    publisher = RecordingPublisher()
    assert OutboxRelay(get_sessionmaker(), publisher, topic=TOPIC).publish_batch() == 2
    assert unpublished_count(db_session) == 0


def test_skip_locked_lets_a_second_relay_skip_claimed_rows(db_session: Session):
    add_events(db_session, 2)
    publisher = RecordingPublisher()

    with get_sessionmaker()() as other_relay, other_relay.begin():
        # Another relay instance has these rows locked mid-publish.
        other_relay.scalars(select(OutboxEvent).with_for_update()).all()
        assert OutboxRelay(get_sessionmaker(), publisher, topic=TOPIC).publish_batch() == 0

    assert publisher.messages == []
    assert OutboxRelay(get_sessionmaker(), publisher, topic=TOPIC).publish_batch() == 2


@pytest.fixture(scope="module")
def kafka_bootstrap() -> Iterator[str]:
    with KafkaContainer("confluentinc/cp-kafka:7.6.0").with_kraft() as kafka:
        bootstrap = kafka.get_bootstrap_server()
        ensure_topic(bootstrap, TOPIC)
        yield bootstrap


def consume_all(bootstrap: str, expected: int, timeout_seconds: float = 20.0) -> list[dict]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"test-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([TOPIC])
    received: list[dict] = []
    try:
        for _ in range(int(timeout_seconds)):
            for message in consumer.consume(num_messages=expected, timeout=1.0):
                assert message.error() is None
                received.append(
                    {"key": message.key().decode(), "value": json.loads(message.value())}
                )
            if len(received) >= expected:
                break
    finally:
        consumer.close()
    return received


def test_completed_transfer_produces_exactly_one_kafka_event(client, kafka_bootstrap: str):
    alice = client.post("/v1/accounts", json={"owner_name": "Alice", "currency": "USD"}).json()
    bob = client.post("/v1/accounts", json={"owner_name": "Bob", "currency": "USD"}).json()
    client.post(
        "/v1/deposits",
        headers={"Idempotency-Key": "fund"},
        json={"account_id": alice["id"], "amount_minor": 100, "currency": "USD"},
    )
    body = {
        "from_account_id": alice["id"],
        "to_account_id": bob["id"],
        "amount_minor": 40,
        "currency": "USD",
    }
    transfer = client.post("/v1/transfers", headers={"Idempotency-Key": "pay"}, json=body).json()
    client.post("/v1/transfers", headers={"Idempotency-Key": "pay"}, json=body)

    relay = OutboxRelay(get_sessionmaker(), KafkaPublisher(kafka_bootstrap), topic=TOPIC)
    assert relay.publish_batch() == 2  # the deposit and the transfer
    assert relay.publish_batch() == 0

    received = consume_all(kafka_bootstrap, expected=2)
    transfer_messages = [m for m in received if m["value"]["aggregate_id"] == transfer["id"]]
    assert len(transfer_messages) == 1
    assert transfer_messages[0]["key"] == alice["id"]
    assert transfer_messages[0]["value"]["event_type"] == "TransferCompleted"
    assert transfer_messages[0]["value"]["data"]["amount_minor"] == 40
